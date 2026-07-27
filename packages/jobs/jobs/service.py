from __future__ import annotations

import csv
import hashlib
import io
import json
import secrets
import sqlite3
import uuid

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fetch.domain.types import Doc
from fetch.sites.registry import SITES

from jobs.database import Database
from jobs.security import (
    SecretCipher,
    digest_submission,
    fingerprint,
    hash_password,
    verify_password,
)
from jobs.settings import Settings
from jobs.storage import LocalObjectStore


class JobsError(Exception):
    """Expected product error suitable for an API response."""


class PermissionDenied(JobsError):
    pass


class NotFound(JobsError):
    pass


class Conflict(JobsError):
    pass


class Cancelled(JobsError):
    # A late checkpoint must be rejected to its caller but the accompanying
    # cancellation fence must remain durable.
    commit_transaction = True


TERMINAL_ITEM_STATES = ("succeeded", "not_found", "exhausted", "cancelled")
PROXY_PROVIDERS = ("geonode", "dataimpulse")
MAX_HEALTHY_CONTACTS = 12
MAX_LEASE_EXPIRIES = 3


@dataclass(frozen=True)
class SubmittedJob:
    id: str
    reused: bool


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def as_time(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class JobsService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.database = Database(settings.database_path)
        self.database.migrate()
        self.objects = LocalObjectStore(settings.object_root)
        self.cipher = SecretCipher(settings.fernet_key)

    # -- bootstrap, identity, and authorization ---------------------------------

    def bootstrap_admin(self) -> str | None:
        email = self.settings.bootstrap_admin_email
        password = self.settings.bootstrap_admin_password
        if not email and not password:
            return None
        if not email or not password:
            msg = "both JOBS_BOOTSTRAP_ADMIN_EMAIL and JOBS_BOOTSTRAP_ADMIN_PASSWORD are required"
            raise RuntimeError(msg)
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT id FROM users WHERE email = ?", (email.lower(),)
            ).fetchone()
            if existing:
                return str(existing["id"])
            user_id = new_id("usr")
            now = as_time(utcnow())
            connection.execute(
                "INSERT INTO users(id,email,password_hash,is_site_admin,created_at) VALUES(?,?,?,?,?)",
                (user_id, email.lower(), hash_password(password), 1, now),
            )
            self._audit(
                connection, "user", user_id, "bootstrap_admin", "user", user_id, "ok"
            )
            return user_id

    def authenticate(self, email: str, password: str) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE email = ?", (email.lower().strip(),)
            ).fetchone()
            if row is None or not verify_password(password, str(row["password_hash"])):
                return None
            return self._user_dict(row)

    def get_user(self, user_id: str) -> dict[str, Any]:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        if row is None:
            raise NotFound("user not found")
        return self._user_dict(row)

    def create_session(self, user_id: str, *, now: datetime | None = None) -> str:
        now = now or utcnow()
        session_id = new_id("ses")
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO sessions(id,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                (session_id, user_id, as_time(now + timedelta(hours=12)), as_time(now)),
            )
        return session_id

    def session_user(
        self, session_id: str, *, now: datetime | None = None
    ) -> dict[str, Any] | None:
        now = now or utcnow()
        with self.database.connection() as connection:
            row = connection.execute(
                """SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id
                WHERE s.id=? AND s.expires_at > ?""",
                (session_id, as_time(now)),
            ).fetchone()
        return self._user_dict(row) if row else None

    def destroy_session(self, session_id: str) -> None:
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    def create_user(
        self, actor_id: str, *, email: str, password: str, site_admin: bool = False
    ) -> dict[str, Any]:
        try:
            password_hash = hash_password(password)
        except ValueError as exc:
            raise Conflict(str(exc)) from exc
        with self.database.transaction() as connection:
            self._require_site_admin(connection, actor_id)
            user_id = new_id("usr")
            now = as_time(utcnow())
            try:
                connection.execute(
                    "INSERT INTO users(id,email,password_hash,is_site_admin,created_at) VALUES(?,?,?,?,?)",
                    (
                        user_id,
                        email.lower().strip(),
                        password_hash,
                        int(site_admin),
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("a user with that email already exists") from exc
            self._audit(
                connection, "user", actor_id, "create_user", "user", user_id, "ok"
            )
            row = connection.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return self._user_dict(row)

    def list_users(self, actor_id: str) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            self._require_site_admin(connection, actor_id)
            rows = connection.execute("SELECT * FROM users ORDER BY email").fetchall()
        return [self._user_dict(row) for row in rows]

    def create_team(self, actor_id: str, *, name: str) -> dict[str, str]:
        name = name.strip()
        if not name:
            raise Conflict("team name is required")
        with self.database.transaction() as connection:
            self._require_site_admin(connection, actor_id)
            team_id = new_id("team")
            try:
                connection.execute(
                    "INSERT INTO teams(id,name,created_at) VALUES(?,?,?)",
                    (team_id, name, as_time(utcnow())),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("a team with that name already exists") from exc
            self._audit(
                connection, "user", actor_id, "create_team", "team", team_id, "ok"
            )
        return {"id": team_id, "name": name}

    def list_teams(self, actor_id: str) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            actor = self._require_user(connection, actor_id)
            if int(actor["is_site_admin"]):
                rows = connection.execute(
                    "SELECT * FROM teams ORDER BY name"
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT t.* FROM teams t JOIN team_memberships m ON m.team_id=t.id
                    WHERE m.user_id=? ORDER BY t.name""",
                    (actor_id,),
                ).fetchall()
        return [{"id": str(row["id"]), "name": str(row["name"])} for row in rows]

    def add_membership(
        self, actor_id: str, *, team_id: str, user_id: str, role: str
    ) -> None:
        if role not in {"leader", "member"}:
            raise Conflict("role must be leader or member")
        with self.database.transaction() as connection:
            self._require_site_admin(connection, actor_id)
            self._require_team(connection, team_id)
            self._require_user(connection, user_id)
            connection.execute(
                """INSERT INTO team_memberships(team_id,user_id,role,created_at) VALUES(?,?,?,?)
                ON CONFLICT(team_id,user_id) DO UPDATE SET role=excluded.role""",
                (team_id, user_id, role, as_time(utcnow())),
            )
            self._audit(
                connection, "user", actor_id, "set_membership", "team", team_id, "ok"
            )

    def remove_membership(self, actor_id: str, *, team_id: str, user_id: str) -> None:
        with self.database.transaction() as connection:
            self._require_site_admin(connection, actor_id)
            connection.execute(
                "DELETE FROM team_memberships WHERE team_id=? AND user_id=?",
                (team_id, user_id),
            )
            self._audit(
                connection, "user", actor_id, "remove_membership", "team", team_id, "ok"
            )

    def team_members(self, actor_id: str, team_id: str) -> list[dict[str, str]]:
        with self.database.connection() as connection:
            self._require_team_access(connection, actor_id, team_id)
            rows = connection.execute(
                """SELECT u.id,u.email,m.role FROM team_memberships m JOIN users u ON u.id=m.user_id
                WHERE m.team_id=? ORDER BY u.email""",
                (team_id,),
            ).fetchall()
        return [
            {"id": str(row["id"]), "email": str(row["email"]), "role": str(row["role"])}
            for row in rows
        ]

    def store_team_credential(
        self,
        actor_id: str,
        *,
        team_id: str,
        provider: str,
        secret_ref: str,
        secret_json: str,
    ) -> None:
        if provider not in PROXY_PROVIDERS:
            raise Conflict("unsupported proxy provider")
        try:
            parsed = json.loads(secret_json)
        except json.JSONDecodeError as exc:
            raise Conflict("credential configuration must be valid JSON") from exc
        if not isinstance(parsed, dict) or not parsed:
            raise Conflict("credential configuration must be a non-empty JSON object")
        if not secret_ref.strip():
            raise Conflict("secret reference is required")
        with self.database.transaction() as connection:
            self._require_site_admin(connection, actor_id)
            self._require_team(connection, team_id)
            credential_id = new_id("cred")
            connection.execute(
                """INSERT INTO team_proxy_credentials(id,team_id,provider,secret_ref,secret_ciphertext,created_by,created_at)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(team_id,provider) DO UPDATE SET secret_ref=excluded.secret_ref,
                secret_ciphertext=excluded.secret_ciphertext,created_by=excluded.created_by,created_at=excluded.created_at""",
                (
                    credential_id,
                    team_id,
                    provider,
                    secret_ref.strip(),
                    self.cipher.encrypt(json.dumps(parsed, separators=(",", ":"))),
                    actor_id,
                    as_time(utcnow()),
                ),
            )
            self._audit(
                connection,
                "user",
                actor_id,
                "store_proxy_credential",
                "team",
                team_id,
                "ok",
            )

    def credential_metadata(self, actor_id: str, team_id: str) -> list[dict[str, str]]:
        with self.database.connection() as connection:
            self._require_team_access(connection, actor_id, team_id)
            rows = connection.execute(
                "SELECT provider,secret_ref,created_at FROM team_proxy_credentials WHERE team_id=?",
                (team_id,),
            ).fetchall()
        return [
            {
                "provider": str(row["provider"]),
                "secret_ref": str(row["secret_ref"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    # -- job creation and read models -------------------------------------------

    def submit_job(
        self,
        actor_id: str,
        *,
        team_id: str,
        sources: list[str],
        provider: str,
        input_bytes: bytes,
        idempotency_key: str,
    ) -> SubmittedJob:
        if not input_bytes:
            raise Conflict("input file is empty")
        if not idempotency_key.strip():
            raise Conflict("idempotency key is required")
        if provider not in PROXY_PROVIDERS:
            raise Conflict("unsupported proxy provider")
        selected = _selected_sources(sources)
        input_checksum = _sha256(input_bytes)
        request_digest = digest_submission(
            input_sha256=input_checksum, sources=selected, provider=provider
        )
        now = utcnow()
        with self.database.transaction() as connection:
            self._require_team_role(connection, actor_id, team_id, {"leader"})
            existing = connection.execute(
                "SELECT id,request_digest FROM jobs WHERE created_by=? AND idempotency_key=?",
                (actor_id, idempotency_key.strip()),
            ).fetchone()
            if existing:
                if str(existing["request_digest"]) != request_digest:
                    raise Conflict(
                        "idempotency key was already used for different input"
                    )
                return SubmittedJob(str(existing["id"]), reused=True)
            credential = connection.execute(
                "SELECT id FROM team_proxy_credentials WHERE team_id=? AND provider=?",
                (team_id, provider),
            ).fetchone()
            if credential is None:
                raise Conflict(
                    "team has no configured credential for the selected provider"
                )

        object_key, checksum = self.objects.put_immutable(
            namespace="restricted-inputs", content=input_bytes
        )
        rows = _parse_input(input_bytes)
        job_id = new_id("job")
        object_id = new_id("obj")
        with self.database.transaction() as connection:
            # A concurrent replay is resolved after the immutable write; its orphaned
            # object is harmless and never referenced by a job.
            existing = connection.execute(
                "SELECT id,request_digest FROM jobs WHERE created_by=? AND idempotency_key=?",
                (actor_id, idempotency_key.strip()),
            ).fetchone()
            if existing:
                if str(existing["request_digest"]) != request_digest:
                    raise Conflict(
                        "idempotency key was already used for different input"
                    )
                return SubmittedJob(str(existing["id"]), reused=True)
            self._require_team_role(connection, actor_id, team_id, {"leader"})
            connection.execute(
                "INSERT INTO immutable_objects(id,namespace,object_key,checksum,content_type,created_at) VALUES(?,?,?,?,?,?)",
                (
                    object_id,
                    "restricted-inputs",
                    object_key,
                    checksum,
                    "text/csv",
                    as_time(now),
                ),
            )
            connection.execute(
                """INSERT INTO jobs(id,team_id,created_by,idempotency_key,request_digest,input_object_id,state,created_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (
                    job_id,
                    team_id,
                    actor_id,
                    idempotency_key.strip(),
                    request_digest,
                    object_id,
                    "queued",
                    as_time(now),
                ),
            )
            for source in selected:
                connection.execute(
                    "INSERT INTO job_sources(job_id,source,provider,adapter_version,policy_version) VALUES(?,?,?,?,?)",
                    (job_id, source, provider, "fetch-0.1.0", "fetch-policy-v1"),
                )
            accepted: set[str] = set()
            for ordinal, raw in enumerate(rows, start=1):
                row_id = new_id("row")
                reason = ""
                document = ""
                try:
                    document = str(Doc(raw))
                except ValueError:
                    reason = "invalid_document"
                if not reason and document in accepted:
                    reason = "duplicate_document"
                if not reason and not any(
                    SITES[source].accepts(Doc(document)) for source in selected
                ):
                    reason = "not_supported_by_selected_sources"
                status = "excluded" if reason else "accepted"
                connection.execute(
                    "INSERT INTO input_rows(id,job_id,ordinal,status,document,value_fingerprint) VALUES(?,?,?,?,?,?)",
                    (
                        row_id,
                        job_id,
                        ordinal,
                        status,
                        document or None,
                        fingerprint(raw, self.settings.session_secret),
                    ),
                )
                if reason:
                    self._record_exclusion(
                        connection, job_id, row_id, ordinal, reason, now
                    )
                    continue
                accepted.add(document)
                for source in selected:
                    if not SITES[source].accepts(Doc(document)):
                        continue
                    item_id = new_id("item")
                    connection.execute(
                        """INSERT INTO work_items(id,job_id,team_id,source,document,document_fingerprint,partition_key,state,version,fence,next_attempt_at,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            item_id,
                            job_id,
                            team_id,
                            source,
                            document,
                            fingerprint(document, self.settings.session_secret),
                            _partition_key(document),
                            "ready",
                            0,
                            0,
                            as_time(now),
                            as_time(now),
                            as_time(now),
                        ),
                    )
            self._audit(connection, "user", actor_id, "submit_job", "job", job_id, "ok")
            self._refresh_job(connection, job_id, now)
        return SubmittedJob(job_id, reused=False)

    def job_view(self, actor_id: str, job_id: str) -> dict[str, Any]:
        with self.database.transaction() as connection:
            job = self._job_with_access(connection, actor_id, job_id)
            summary = self._summary(connection, job_id)
            self._audit(connection, "user", actor_id, "view_job", "job", job_id, "ok")
        return {
            **_job_dict(job),
            "summary": summary,
            "sources": self._job_sources(job_id),
        }

    def job_exclusions(self, actor_id: str, job_id: str) -> list[dict[str, Any]]:
        with self.database.transaction() as connection:
            job = self._job_with_access(connection, actor_id, job_id, leaders_only=True)
            rows = connection.execute(
                "SELECT ordinal,reason FROM input_exclusions WHERE job_id=? ORDER BY ordinal",
                (job["id"],),
            ).fetchall()
            self._audit(
                connection, "user", actor_id, "view_exclusions", "job", job_id, "ok"
            )
        return [
            {"ordinal": int(row["ordinal"]), "reason": str(row["reason"])}
            for row in rows
        ]

    def list_jobs(self, actor_id: str, team_id: str) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            self._require_team_access(connection, actor_id, team_id)
            rows = connection.execute(
                "SELECT * FROM jobs WHERE team_id=? ORDER BY created_at DESC",
                (team_id,),
            ).fetchall()
            return [
                {**_job_dict(row), "summary": self._summary(connection, str(row["id"]))}
                for row in rows
            ]

    def cancel_job(
        self, actor_id: str, job_id: str, *, now: datetime | None = None
    ) -> dict[str, Any]:
        now = now or utcnow()
        with self.database.transaction() as connection:
            job = self._job_with_access(connection, actor_id, job_id, leaders_only=True)
            if str(job["state"]) in {"completed", "cancelled", "system_failed"}:
                raise Conflict("job is already terminal")
            connection.execute(
                "UPDATE jobs SET state='cancelling',cancelled_at=? WHERE id=?",
                (as_time(now), job_id),
            )
            connection.execute(
                """UPDATE work_items SET state='cancelled',version=version+1,updated_at=?
                WHERE job_id=? AND state IN ('ready','retry_wait')""",
                (as_time(now), job_id),
            )
            self._audit(connection, "user", actor_id, "cancel_job", "job", job_id, "ok")
            self._refresh_job(connection, job_id, now)
            return self._summary(connection, job_id)

    def search_results(
        self,
        actor_id: str,
        *,
        team_id: str,
        document: str = "",
        source: str = "",
    ) -> list[dict[str, Any]]:
        with self.database.transaction() as connection:
            self._require_team_access(connection, actor_id, team_id)
            clauses = ["team_id=?"]
            values: list[str] = [team_id]
            if document.strip():
                clauses.append("document LIKE ?")
                values.append(f"{document.strip()}%")
            if source.strip():
                if source not in SITES:
                    raise Conflict("unknown source")
                clauses.append("source=?")
                values.append(source)
            rows = connection.execute(
                f"""SELECT id,job_id,source,document,status,payload_json,published_at
                FROM results WHERE {" AND ".join(clauses)} ORDER BY published_at DESC LIMIT 200""",
                values,
            ).fetchall()
            self._audit(
                connection, "user", actor_id, "search_results", "team", team_id, "ok"
            )
        return [
            {
                "id": str(row["id"]),
                "job_id": str(row["job_id"]),
                "source": str(row["source"]),
                "document": str(row["document"]),
                "status": str(row["status"]),
                "payload": json.loads(str(row["payload_json"])),
                "published_at": str(row["published_at"]),
            }
            for row in rows
        ]

    def create_export(self, actor_id: str, job_id: str) -> dict[str, str]:
        with self.database.transaction() as connection:
            self._job_with_access(connection, actor_id, job_id)
            rows = connection.execute(
                "SELECT source,document,status,payload_json FROM results WHERE job_id=? ORDER BY source,document",
                (job_id,),
            ).fetchall()
            output = io.StringIO(newline="")
            writer = csv.writer(output)
            writer.writerow(("source", "document", "status", "payload"))
            for row in rows:
                writer.writerow(
                    (
                        row["source"],
                        row["document"],
                        row["status"],
                        row["payload_json"],
                    )
                )
            payload = output.getvalue().encode()
        object_key, checksum = self.objects.put_immutable(
            namespace="exports", content=payload
        )
        export_id = new_id("export")
        object_id = new_id("obj")
        with self.database.transaction() as connection:
            self._job_with_access(connection, actor_id, job_id)
            connection.execute(
                "INSERT INTO immutable_objects(id,namespace,object_key,checksum,content_type,created_at) VALUES(?,?,?,?,?,?)",
                (
                    object_id,
                    "exports",
                    object_key,
                    checksum,
                    "text/csv",
                    as_time(utcnow()),
                ),
            )
            connection.execute(
                "INSERT INTO exports(id,job_id,object_id,filters_json,schema_version,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    export_id,
                    job_id,
                    object_id,
                    "{}",
                    "jobs-export-v1",
                    actor_id,
                    as_time(utcnow()),
                ),
            )
            self._audit(
                connection, "user", actor_id, "create_export", "export", export_id, "ok"
            )
        return {"id": export_id, "checksum": checksum}

    def read_export(self, actor_id: str, export_id: str) -> bytes:
        with self.database.transaction() as connection:
            row = connection.execute(
                """SELECT e.id,e.job_id,o.object_key FROM exports e JOIN immutable_objects o ON o.id=e.object_id
                WHERE e.id=?""",
                (export_id,),
            ).fetchone()
            if row is None:
                raise NotFound("export not found")
            self._job_with_access(connection, actor_id, str(row["job_id"]))
            self._audit(
                connection,
                "user",
                actor_id,
                "download_export",
                "export",
                export_id,
                "ok",
            )
            key = str(row["object_key"])
        return self.objects.read(key)

    def notifications(self, actor_id: str, team_id: str) -> list[dict[str, str]]:
        with self.database.transaction() as connection:
            self._require_team_access(connection, actor_id, team_id)
            rows = connection.execute(
                """SELECT n.id,n.job_id,n.event_type,n.delivery_state,n.created_at FROM notification_outbox n
                JOIN jobs j ON j.id=n.job_id WHERE j.team_id=? AND n.channel='in_app'
                ORDER BY n.created_at DESC LIMIT 100""",
                (team_id,),
            ).fetchall()
            self._audit(
                connection,
                "user",
                actor_id,
                "view_notifications",
                "team",
                team_id,
                "ok",
            )
        return [
            {
                "id": str(row["id"]),
                "job_id": str(row["job_id"]),
                "event_type": str(row["event_type"]),
                "delivery_state": str(row["delivery_state"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    # -- outbound worker / lease protocol ----------------------------------------

    def register_worker(
        self,
        *,
        worker_id: str,
        bootstrap_token: str,
        sources: list[str],
        capacity: int,
    ) -> None:
        if not self.settings.worker_bootstrap_token:
            raise PermissionDenied("worker registration is not configured")
        if not secrets.compare_digest(
            bootstrap_token, self.settings.worker_bootstrap_token
        ):
            raise PermissionDenied("invalid worker credential")
        selected = _selected_sources(sources)
        if capacity < 1:
            raise Conflict("worker capacity must be at least one")
        now = as_time(utcnow())
        token_hash = _sha256(bootstrap_token.encode())
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO workers(id,token_hash,capabilities_json,capacity,last_seen_at,created_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET token_hash=excluded.token_hash,
                capabilities_json=excluded.capabilities_json,capacity=excluded.capacity,
                last_seen_at=excluded.last_seen_at,revoked_at=NULL""",
                (worker_id, token_hash, json.dumps(selected), capacity, now, now),
            )
            self._audit(
                connection, "worker", worker_id, "register", "worker", worker_id, "ok"
            )

    def claim_work(
        self,
        *,
        worker_id: str,
        worker_token: str,
        max_items: int,
        lease_seconds: int = 60,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        if max_items < 1 or max_items > 100:
            raise Conflict("max_items must be between 1 and 100")
        now = now or utcnow()
        expiry = now + timedelta(seconds=lease_seconds)
        with self.database.transaction() as connection:
            worker = self._require_worker(connection, worker_id, worker_token)
            self._sweep_expired(connection, now)
            connection.execute(
                """UPDATE work_items SET state='ready',version=version+1,updated_at=?
                WHERE state='retry_wait' AND next_attempt_at <= ?
                AND job_id IN (SELECT id FROM jobs WHERE state='running')""",
                (as_time(now), as_time(now)),
            )
            self._promote_queued_jobs(connection, now)
            capabilities = json.loads(str(worker["capabilities_json"]))
            placeholders = ",".join("?" for _ in capabilities)
            rows = connection.execute(
                f"""SELECT w.*,js.provider FROM work_items w JOIN jobs j ON j.id=w.job_id
                JOIN job_sources js ON js.job_id=w.job_id AND js.source=w.source
                WHERE j.state='running' AND w.state='ready' AND w.next_attempt_at <= ?
                AND w.source IN ({placeholders})
                ORDER BY j.created_at,w.partition_key,w.created_at LIMIT ?""",
                [as_time(now), *capabilities, max_items],
            ).fetchall()
            claimed: list[dict[str, Any]] = []
            for row in rows:
                lease_id = new_id("lease")
                fence = int(row["fence"]) + 1
                new_version = int(row["version"]) + 1
                update = connection.execute(
                    """UPDATE work_items SET state='leased',version=?,fence=?,lease_id=?,lease_expires_at=?,updated_at=?
                    WHERE id=? AND state='ready' AND version=?""",
                    (
                        new_version,
                        fence,
                        lease_id,
                        as_time(expiry),
                        as_time(now),
                        row["id"],
                        row["version"],
                    ),
                )
                if update.rowcount != 1:
                    continue
                connection.execute(
                    """INSERT INTO leases(id,work_item_id,job_id,worker_id,fence,expires_at,state,execution_id,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        lease_id,
                        row["id"],
                        row["job_id"],
                        worker_id,
                        fence,
                        as_time(expiry),
                        "active",
                        new_id("exec"),
                        as_time(now),
                        as_time(now),
                    ),
                )
                claimed.append(
                    {
                        "lease_id": lease_id,
                        "work_item_id": str(row["id"]),
                        "job_id": str(row["job_id"]),
                        "team_id": str(row["team_id"]),
                        "source": str(row["source"]),
                        "document": str(row["document"]),
                        "provider": str(row["provider"]),
                        "fence": fence,
                        "version": new_version,
                        "expires_at": as_time(expiry),
                    }
                )
            connection.execute(
                "UPDATE workers SET last_seen_at=? WHERE id=?",
                (as_time(now), worker_id),
            )
            self._audit(
                connection, "worker", worker_id, "claim", "worker", worker_id, "ok"
            )
        return claimed

    def renew_lease(
        self,
        *,
        worker_id: str,
        worker_token: str,
        lease_id: str,
        fence: int,
        lease_seconds: int = 60,
        now: datetime | None = None,
    ) -> str:
        now = now or utcnow()
        expiry = now + timedelta(seconds=lease_seconds)
        with self.database.transaction() as connection:
            self._require_worker(connection, worker_id, worker_token)
            row = connection.execute(
                """SELECT l.*,j.state AS job_state FROM leases l JOIN jobs j ON j.id=l.job_id
                WHERE l.id=? AND l.worker_id=?""",
                (lease_id, worker_id),
            ).fetchone()
            if (
                row is None
                or int(row["fence"]) != fence
                or str(row["state"]) != "active"
            ):
                raise Conflict("lease is no longer current")
            if str(row["job_state"]) != "running" or str(row["expires_at"]) <= as_time(
                now
            ):
                raise Cancelled("lease was cancelled or expired")
            connection.execute(
                "UPDATE leases SET expires_at=?,updated_at=? WHERE id=?",
                (as_time(expiry), as_time(now), lease_id),
            )
            connection.execute(
                "UPDATE work_items SET lease_expires_at=?,updated_at=? WHERE lease_id=?",
                (as_time(expiry), as_time(now), lease_id),
            )
        return as_time(expiry)

    def lease_cancelled(
        self, *, worker_id: str, worker_token: str, lease_id: str
    ) -> bool:
        with self.database.transaction() as connection:
            self._require_worker(connection, worker_id, worker_token)
            row = connection.execute(
                """SELECT j.state FROM leases l JOIN jobs j ON j.id=l.job_id
                WHERE l.id=? AND l.worker_id=?""",
                (lease_id, worker_id),
            ).fetchone()
            if row is None:
                raise NotFound("lease not found")
            return str(row["state"]) != "running"

    def lease_credential(
        self, *, worker_id: str, worker_token: str, lease_id: str
    ) -> dict[str, Any]:
        """Return a team's decrypted proxy configuration only to its active worker.

        This narrow endpoint is deliberately absent from human UI/API read models.
        Workers should hold it in memory only and use TLS in deployment.
        """
        with self.database.transaction() as connection:
            self._require_worker(connection, worker_id, worker_token)
            row = connection.execute(
                """SELECT c.provider,c.secret_ref,c.secret_ciphertext FROM leases l
                JOIN work_items w ON w.id=l.work_item_id
                JOIN job_sources js ON js.job_id=w.job_id AND js.source=w.source
                JOIN team_proxy_credentials c ON c.team_id=w.team_id AND c.provider=js.provider
                WHERE l.id=? AND l.worker_id=? AND l.state='active'""",
                (lease_id, worker_id),
            ).fetchone()
            if row is None:
                raise PermissionDenied("no active credential grant for lease")
            payload = json.loads(self.cipher.decrypt(bytes(row["secret_ciphertext"])))
            return {
                "provider": str(row["provider"]),
                "secret_ref": str(row["secret_ref"]),
                "config": payload,
            }

    def checkpoint(
        self,
        *,
        worker_id: str,
        worker_token: str,
        lease_id: str,
        work_item_id: str,
        fence: int,
        version: int,
        attempt_id: str,
        sequence: int,
        outcome: str,
        payload: dict[str, Any] | None = None,
        error_code: str = "",
        healthy_contact_delta: int = 0,
        retry_after_s: int = 30,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if outcome not in {
            "succeeded",
            "not_found",
            "retryable",
            "exhausted",
            "cancelled",
        }:
            raise Conflict("invalid checkpoint outcome")
        if healthy_contact_delta < 0 or healthy_contact_delta > 4:
            raise Conflict("healthy contact delta must be between 0 and 4")
        if sequence < 1:
            raise Conflict("attempt sequence must be positive")
        now = now or utcnow()
        with self.database.transaction() as connection:
            self._require_worker(connection, worker_id, worker_token)
            prior = connection.execute(
                "SELECT id FROM source_attempts WHERE id=?", (attempt_id,)
            ).fetchone()
            if prior:
                return {"accepted": True, "duplicate": True}
            item = connection.execute(
                """SELECT w.*,l.worker_id,l.fence AS lease_fence,l.expires_at,l.state AS lease_state,j.state AS job_state
                FROM work_items w JOIN leases l ON l.id=w.lease_id JOIN jobs j ON j.id=w.job_id
                WHERE w.id=? AND l.id=?""",
                (work_item_id, lease_id),
            ).fetchone()
            if item is None:
                raise Conflict("stale or unknown lease")
            if (
                str(item["worker_id"]) != worker_id
                or int(item["lease_fence"]) != fence
                or int(item["version"]) != version
                or str(item["lease_state"]) != "active"
                or str(item["lease_expires_at"]) <= as_time(now)
            ):
                raise Conflict("stale checkpoint rejected by lease fence")
            if str(item["job_state"]) != "running":
                connection.execute(
                    "UPDATE work_items SET state='cancelled',version=version+1,updated_at=? WHERE id=?",
                    (as_time(now), work_item_id),
                )
                connection.execute(
                    "UPDATE leases SET state='cancelled',updated_at=? WHERE id=?",
                    (as_time(now), lease_id),
                )
                self._refresh_job(connection, str(item["job_id"]), now)
                raise Cancelled("job cancellation fenced this checkpoint")
            detail = _safe_error_code(error_code)
            connection.execute(
                """INSERT INTO source_attempts(id,work_item_id,lease_id,sequence,classification,healthy_contact_delta,sanitized_detail,created_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (
                    attempt_id,
                    work_item_id,
                    lease_id,
                    sequence,
                    outcome,
                    healthy_contact_delta,
                    detail,
                    as_time(now),
                ),
            )
            contacts = int(item["healthy_contacts"]) + healthy_contact_delta
            new_state, next_attempt = _checkpoint_transition(
                outcome, contacts=contacts, now=now, retry_after_s=retry_after_s
            )
            connection.execute(
                """UPDATE work_items SET state=?,version=version+1,healthy_contacts=?,attempt_count=attempt_count+1,
                lease_expiry_count=0,next_attempt_at=?,lease_id=NULL,lease_expires_at=NULL,error_code=?,updated_at=? WHERE id=?""",
                (
                    new_state,
                    contacts,
                    as_time(next_attempt),
                    detail or None,
                    as_time(now),
                    work_item_id,
                ),
            )
            connection.execute(
                "UPDATE leases SET state='closed',updated_at=? WHERE id=?",
                (as_time(now), lease_id),
            )
            if new_state in {"succeeded", "not_found"}:
                result_id = new_id("result")
                connection.execute(
                    """INSERT INTO results(id,work_item_id,job_id,team_id,source,document,status,payload_json,schema_version,published_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        result_id,
                        work_item_id,
                        item["job_id"],
                        item["team_id"],
                        item["source"],
                        item["document"],
                        new_state,
                        json.dumps(payload or {}, separators=(",", ":")),
                        "fetch-result-v1",
                        as_time(now),
                    ),
                )
            self._audit(
                connection,
                "worker",
                worker_id,
                "checkpoint",
                "work_item",
                work_item_id,
                "ok",
            )
            self._refresh_job(connection, str(item["job_id"]), now)
        return {"accepted": True, "duplicate": False, "state": new_state}

    def sweep_expired_leases(self, *, now: datetime | None = None) -> int:
        now = now or utcnow()
        with self.database.transaction() as connection:
            return self._sweep_expired(connection, now)

    # -- transaction-private helpers ---------------------------------------------

    def _require_user(
        self, connection: sqlite3.Connection, user_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM users WHERE id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise PermissionDenied("authentication required")
        return row

    def _require_site_admin(
        self, connection: sqlite3.Connection, user_id: str
    ) -> sqlite3.Row:
        row = self._require_user(connection, user_id)
        if not int(row["is_site_admin"]):
            raise PermissionDenied("site administrator role is required")
        return row

    def _require_team(
        self, connection: sqlite3.Connection, team_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM teams WHERE id=?", (team_id,)
        ).fetchone()
        if row is None:
            raise NotFound("team not found")
        return row

    def _require_team_access(
        self, connection: sqlite3.Connection, user_id: str, team_id: str
    ) -> str:
        user = self._require_user(connection, user_id)
        self._require_team(connection, team_id)
        if int(user["is_site_admin"]):
            return "site_admin"
        row = connection.execute(
            "SELECT role FROM team_memberships WHERE team_id=? AND user_id=?",
            (team_id, user_id),
        ).fetchone()
        if row is None:
            raise PermissionDenied("team membership is required")
        return str(row["role"])

    def _require_team_role(
        self,
        connection: sqlite3.Connection,
        user_id: str,
        team_id: str,
        roles: set[str],
    ) -> str:
        role = self._require_team_access(connection, user_id, team_id)
        if role == "site_admin" or role in roles:
            return role
        raise PermissionDenied("team leader role is required")

    def _job_with_access(
        self,
        connection: sqlite3.Connection,
        actor_id: str,
        job_id: str,
        *,
        leaders_only: bool = False,
    ) -> sqlite3.Row:
        job = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job is None:
            raise NotFound("job not found")
        if leaders_only:
            self._require_team_role(
                connection, actor_id, str(job["team_id"]), {"leader"}
            )
        else:
            self._require_team_access(connection, actor_id, str(job["team_id"]))
        return job

    def _require_worker(
        self, connection: sqlite3.Connection, worker_id: str, token: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM workers WHERE id=?", (worker_id,)
        ).fetchone()
        if row is None or row["revoked_at"] is not None:
            raise PermissionDenied("worker is not registered")
        if not secrets.compare_digest(str(row["token_hash"]), _sha256(token.encode())):
            raise PermissionDenied("invalid worker credential")
        return row

    def _record_exclusion(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        row_id: str,
        ordinal: int,
        reason: str,
        now: datetime,
    ) -> None:
        connection.execute(
            "INSERT INTO input_exclusions(id,job_id,input_row_id,ordinal,reason,created_at) VALUES(?,?,?,?,?,?)",
            (new_id("exclude"), job_id, row_id, ordinal, reason, as_time(now)),
        )

    def _summary(self, connection: sqlite3.Connection, job_id: str) -> dict[str, int]:
        counts = dict.fromkeys(
            (*TERMINAL_ITEM_STATES, "ready", "leased", "retry_wait"), 0
        )
        rows = connection.execute(
            "SELECT state,COUNT(*) AS count FROM work_items WHERE job_id=? GROUP BY state",
            (job_id,),
        ).fetchall()
        for row in rows:
            counts[str(row["state"])] = int(row["count"])
        excluded = connection.execute(
            "SELECT COUNT(*) AS count FROM input_exclusions WHERE job_id=?", (job_id,)
        ).fetchone()
        return {
            "succeeded": counts["succeeded"],
            "not_found": counts["not_found"],
            "excluded": int(excluded["count"]),
            "exhausted_or_failed": counts["exhausted"],
            "cancelled": counts["cancelled"],
            "remaining": counts["ready"] + counts["leased"] + counts["retry_wait"],
            "ready": counts["ready"],
            "leased": counts["leased"],
            "retry_wait": counts["retry_wait"],
        }

    def _job_sources(self, job_id: str) -> list[str]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT source FROM job_sources WHERE job_id=? ORDER BY source",
                (job_id,),
            ).fetchall()
        return [str(row["source"]) for row in rows]

    def _refresh_job(
        self, connection: sqlite3.Connection, job_id: str, now: datetime
    ) -> None:
        job = connection.execute(
            "SELECT state FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if job is None or str(job["state"]) in {
            "completed",
            "cancelled",
            "system_failed",
        }:
            return
        summary = self._summary(connection, job_id)
        next_state: str | None = None
        if str(job["state"]) == "cancelling" and summary["leased"] == 0:
            next_state = "cancelled"
        elif str(job["state"]) != "cancelling" and summary["remaining"] == 0:
            next_state = "completed"
        if next_state is None:
            return
        connection.execute(
            "UPDATE jobs SET state=?,terminal_at=? WHERE id=?",
            (next_state, as_time(now), job_id),
        )
        self._queue_terminal_notifications(connection, job_id, next_state, summary, now)

    def _queue_terminal_notifications(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        state: str,
        summary: dict[str, int],
        now: datetime,
    ) -> None:
        payload = json.dumps(
            {"job_id": job_id, "state": state, "summary": summary},
            separators=(",", ":"),
        )
        channels = (
            ("in_app", True),
            ("email", self.settings.external_email_enabled),
            ("kapso_whatsapp", self.settings.external_whatsapp_enabled),
        )
        for channel, enabled in channels:
            connection.execute(
                """INSERT OR IGNORE INTO notification_outbox(id,job_id,channel,event_type,payload_json,delivery_state,external_enabled,created_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (
                    new_id("outbox"),
                    job_id,
                    channel,
                    f"job_{state}",
                    payload,
                    "pending" if enabled or channel == "in_app" else "disabled",
                    int(enabled),
                    as_time(now),
                ),
            )

    def _promote_queued_jobs(
        self, connection: sqlite3.Connection, now: datetime
    ) -> None:
        # Empty jobs can become terminal before consuming a global active-job slot.
        empty_jobs = connection.execute(
            """SELECT j.id FROM jobs j WHERE j.state='queued'
            AND NOT EXISTS(SELECT 1 FROM work_items w WHERE w.job_id=j.id)"""
        ).fetchall()
        for job in empty_jobs:
            self._refresh_job(connection, str(job["id"]), now)
        active_row = connection.execute(
            "SELECT COUNT(*) AS count FROM jobs WHERE state IN ('running','cancelling')"
        ).fetchone()
        available = self.settings.active_job_limit - int(active_row["count"])
        if available <= 0:
            return
        queued = connection.execute(
            """SELECT j.id FROM jobs j WHERE j.state='queued' AND EXISTS(
            SELECT 1 FROM work_items w WHERE w.job_id=j.id AND w.state IN ('ready','retry_wait'))
            ORDER BY j.created_at LIMIT ?""",
            (available,),
        ).fetchall()
        for job in queued:
            connection.execute(
                "UPDATE jobs SET state='running' WHERE id=?", (job["id"],)
            )
            self._audit(
                connection,
                "system",
                "scheduler",
                "start_job",
                "job",
                str(job["id"]),
                "ok",
            )

    def _sweep_expired(self, connection: sqlite3.Connection, now: datetime) -> int:
        leases = connection.execute(
            """SELECT l.id,l.work_item_id,l.job_id,j.state AS job_state,w.lease_expiry_count
            FROM leases l JOIN jobs j ON j.id=l.job_id JOIN work_items w ON w.id=l.work_item_id
            WHERE l.state='active' AND l.expires_at <= ?""",
            (as_time(now),),
        ).fetchall()
        for lease in leases:
            expiry_count = int(lease["lease_expiry_count"]) + 1
            is_cancelling = str(lease["job_state"]) == "cancelling"
            is_exhausted = not is_cancelling and expiry_count >= MAX_LEASE_EXPIRIES
            state = (
                "cancelled"
                if is_cancelling
                else "exhausted"
                if is_exhausted
                else "ready"
            )
            connection.execute(
                """UPDATE work_items SET state=?,version=version+1,lease_expiry_count=?,lease_id=NULL,lease_expires_at=NULL,error_code=?,updated_at=?
                WHERE id=? AND lease_id=?""",
                (
                    state,
                    expiry_count,
                    "worker_lease_expired" if is_exhausted else None,
                    as_time(now),
                    lease["work_item_id"],
                    lease["id"],
                ),
            )
            connection.execute(
                "UPDATE leases SET state=?,updated_at=? WHERE id=?",
                (
                    "cancelled" if state == "cancelled" else "expired",
                    as_time(now),
                    lease["id"],
                ),
            )
            self._audit(
                connection,
                "system",
                "sweeper",
                "expire_lease",
                "lease",
                str(lease["id"]),
                "ok",
            )
            self._refresh_job(connection, str(lease["job_id"]), now)
        return len(leases)

    def _audit(
        self,
        connection: sqlite3.Connection,
        actor_type: str,
        actor_id: str,
        action: str,
        object_type: str,
        object_id: str,
        outcome: str,
    ) -> None:
        connection.execute(
            """INSERT INTO audit_events(id,actor_type,actor_id,action,object_type,object_id,outcome,correlation_id,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                new_id("audit"),
                actor_type,
                actor_id,
                action,
                object_type,
                object_id,
                outcome,
                new_id("corr"),
                as_time(utcnow()),
            ),
        )

    @staticmethod
    def _user_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "email": str(row["email"]),
            "is_site_admin": bool(row["is_site_admin"]),
        }


def _selected_sources(sources: Iterable[str]) -> list[str]:
    selected = sorted({source.strip().lower() for source in sources if source.strip()})
    if not selected:
        raise Conflict("select at least one stable fetch source")
    unknown = [source for source in selected if source not in SITES]
    if unknown:
        raise Conflict(f"unsupported source: {','.join(unknown)}")
    return selected


def _parse_input(content: bytes) -> list[str]:
    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise Conflict("input must be UTF-8 CSV") from exc
    rows: list[str] = []
    for row in csv.reader(io.StringIO(decoded)):
        # A non-single-column row is handled as an explicit invalid value, rather
        # than silently accepting only its first field.
        rows.append(row[0].strip() if len(row) == 1 else "")
    if not rows:
        raise Conflict("input contains no rows")
    return rows


def _partition_key(document: str) -> int:
    return int(_sha256(document.encode())[:8], 16) % 128


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_error_code(value: str) -> str:
    # Do not persist arbitrary exception text: it may contain a URL, a response
    # fragment, or a secret supplied by a provider. Error codes are operationally
    # useful but deliberately low-cardinality.
    return "".join(
        character
        for character in value.lower()
        if character.isalnum() or character in "_-"
    )[:80]


def _checkpoint_transition(
    outcome: str, *, contacts: int, now: datetime, retry_after_s: int
) -> tuple[str, datetime]:
    if outcome == "succeeded":
        return "succeeded", now
    if outcome == "not_found":
        return "not_found", now
    if outcome == "cancelled":
        return "cancelled", now
    if outcome == "exhausted" or contacts >= MAX_HEALTHY_CONTACTS:
        return "exhausted", now
    bounded_delay = min(max(retry_after_s, 1), 3600)
    return "retry_wait", now + timedelta(seconds=bounded_delay)


def _job_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "team_id": str(row["team_id"]),
        "created_by": str(row["created_by"]),
        "state": str(row["state"]),
        "created_at": str(row["created_at"]),
        "cancelled_at": row["cancelled_at"],
        "terminal_at": row["terminal_at"],
    }
