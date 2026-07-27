from __future__ import annotations

import sqlite3

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


_MIGRATION_001 = """
CREATE TABLE users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    is_site_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX sessions_user_idx ON sessions(user_id);
CREATE TABLE teams (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE team_memberships (
    team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('leader', 'member')),
    created_at TEXT NOT NULL,
    PRIMARY KEY(team_id, user_id)
);
CREATE TABLE team_proxy_credentials (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    provider TEXT NOT NULL CHECK(provider IN ('geonode', 'dataimpulse')),
    secret_ref TEXT NOT NULL,
    secret_ciphertext BLOB NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    UNIQUE(team_id, provider)
);
CREATE TABLE immutable_objects (
    id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    object_key TEXT NOT NULL UNIQUE,
    checksum TEXT NOT NULL,
    content_type TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES teams(id),
    created_by TEXT NOT NULL REFERENCES users(id),
    idempotency_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    input_object_id TEXT NOT NULL REFERENCES immutable_objects(id),
    state TEXT NOT NULL CHECK(state IN ('queued', 'running', 'cancelling', 'completed', 'cancelled', 'system_failed')),
    created_at TEXT NOT NULL,
    cancelled_at TEXT,
    terminal_at TEXT,
    UNIQUE(created_by, idempotency_key)
);
CREATE INDEX jobs_team_idx ON jobs(team_id, created_at DESC);
CREATE TABLE job_sources (
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    provider TEXT NOT NULL CHECK(provider IN ('geonode', 'dataimpulse')),
    adapter_version TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    PRIMARY KEY(job_id, source)
);
CREATE TABLE input_rows (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('accepted', 'excluded')),
    document TEXT,
    value_fingerprint TEXT NOT NULL,
    UNIQUE(job_id, ordinal)
);
CREATE TABLE input_exclusions (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    input_row_id TEXT NOT NULL REFERENCES input_rows(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(job_id, ordinal)
);
CREATE TABLE work_items (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    team_id TEXT NOT NULL REFERENCES teams(id),
    source TEXT NOT NULL,
    document TEXT NOT NULL,
    document_fingerprint TEXT NOT NULL,
    partition_key INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('ready', 'leased', 'retry_wait', 'succeeded', 'not_found', 'exhausted', 'cancelled')),
    version INTEGER NOT NULL DEFAULT 0,
    fence INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    healthy_contacts INTEGER NOT NULL DEFAULT 0,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    lease_id TEXT,
    lease_expires_at TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(job_id, source, document_fingerprint)
);
CREATE INDEX work_claim_idx ON work_items(state, next_attempt_at, source);
CREATE INDEX work_job_idx ON work_items(job_id, state);
CREATE TABLE leases (
    id TEXT PRIMARY KEY,
    work_item_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    worker_id TEXT NOT NULL,
    fence INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('active', 'closed', 'expired', 'cancelled')),
    execution_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX leases_expiry_idx ON leases(state, expires_at);
CREATE TABLE source_attempts (
    id TEXT PRIMARY KEY,
    work_item_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
    lease_id TEXT NOT NULL REFERENCES leases(id),
    sequence INTEGER NOT NULL,
    classification TEXT NOT NULL,
    healthy_contact_delta INTEGER NOT NULL DEFAULT 0,
    sanitized_detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(lease_id, work_item_id, sequence)
);
CREATE TABLE results (
    id TEXT PRIMARY KEY,
    work_item_id TEXT NOT NULL UNIQUE REFERENCES work_items(id) ON DELETE CASCADE,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    team_id TEXT NOT NULL REFERENCES teams(id),
    source TEXT NOT NULL,
    document TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('succeeded', 'not_found')),
    payload_json TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    published_at TEXT NOT NULL
);
CREATE INDEX results_team_search_idx ON results(team_id, source, document);
CREATE TABLE exports (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    object_id TEXT NOT NULL REFERENCES immutable_objects(id),
    filters_json TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE TABLE workers (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    capacity INTEGER NOT NULL,
    revoked_at TEXT,
    last_seen_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE audit_events (
    id TEXT PRIMARY KEY,
    actor_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX audit_object_idx ON audit_events(object_type, object_id, created_at);
CREATE TABLE notification_outbox (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    channel TEXT NOT NULL CHECK(channel IN ('in_app', 'email', 'kapso_whatsapp')),
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    delivery_state TEXT NOT NULL CHECK(delivery_state IN ('pending', 'delivered', 'disabled')),
    external_enabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    UNIQUE(job_id, channel, event_type)
);
"""

_MIGRATIONS = ((1, _MIGRATION_001),)


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def migrate(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)"
            )
            for version, sql in _MIGRATIONS:
                applied = connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
                ).fetchone()
                if applied is None:
                    connection.executescript(sql)
                    connection.execute(
                        "INSERT INTO schema_migrations(version) VALUES (?)", (version,)
                    )

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException as exc:
                if getattr(exc, "commit_transaction", False):
                    connection.execute("COMMIT")
                else:
                    connection.execute("ROLLBACK")
                raise
            else:
                connection.execute("COMMIT")
