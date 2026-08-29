from __future__ import annotations

import asyncio
import base64
import os
import re

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import aioboto3
import asyncpg
import pyotp
import pytest

from botocore.exceptions import ClientError
from litestar.testing import AsyncTestClient, TestClient
from portal.application.provisioning import ProvisioningService
from portal.application.service import PortalService
from portal.application.sessions import BrowserSessions, OneTimeTokens
from portal.credentials.masterkey import MasterKeyring
from portal.credentials.secrets import EnvelopeProtector
from portal.domain.models import (
    AttemptRecord,
    ClaimedWork,
    InputLine,
    ProtectedSecret,
    SubmitJob,
    TeamRole,
)
from portal.ephemeral import EphemeralStore
from portal.migrations import apply_migrations
from portal.notify.mailer import ConsoleMailer
from portal.repository.audit import PostgresAuditLog
from portal.repository.auth import PostgresAuthRepository
from portal.repository.credentials import PostgresCredentialRepository
from portal.repository.entries import PostgresEntryRepository
from portal.repository.jobs import PostgresJobRepository
from portal.repository.search_log import PostgresSearchLogRepository
from portal.repository.teams import PostgresTeamRepository
from portal.repository.workers import PostgresWorkerRegistry
from portal.security import hash_password, new_worker_credential, token_hash
from portal.settings import PortalSettings
from portal.web.app import create_web_app
from portal.worker.api import create_worker_api


if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    import httpx

    from litestar import Litestar


POSTGRES_DSN = (
    os.environ.get("PORTAL_TEST_DSN") or "postgresql://postgres@127.0.0.1:5432/postgres"
)

OBJECT_STORAGE_ENDPOINT = (
    os.environ.get("PORTAL_TEST_OBJECT_STORAGE_ENDPOINT") or "http://127.0.0.1:9100"
)
OBJECT_STORAGE_BUCKET = "portal-objects"
OBJECT_STORAGE_ACCESS_KEY = "portal-dev"
OBJECT_STORAGE_SECRET_KEY = "portal-dev-secret"

MASTER_KEY_VERSION = "v1"
MASTER_KEY = base64.urlsafe_b64encode(b"c" * 32).decode("ascii")

# The same key material the app fixtures load from disk, for seeding helpers
# that run outside a fixture. Both open each other's envelopes.
KEYRING = MasterKeyring.from_lines([f"{MASTER_KEY_VERSION} {MASTER_KEY}"])

# https, like production: Secure cookies and the __Host- prefix are not gated on
# the environment, so a test over http would exercise a shape nobody deploys.
ORIGIN = "https://testserver.local"
PASSWORD = "una-clave-larga-y-segura"

# Fixed so a test can predict the code the authenticator would show.
TOTP_SECRET = "JBSWY3DPEHPK3PXP"
RECOVERY_CODE = "codigo-de-recuperacion"
WORKER_ID = "poseidon-1"
WORKER_HOSTNAME = "poseidon-1.tailnet.ts.net"

# Stands in for a real envelope where the test never decrypts it.
UNREADABLE_SECRET = ProtectedSecret(b"cifrado", b"\x00", MASTER_KEY_VERSION)


@pytest.fixture(scope="session")
def portal_cluster() -> str:
    async def ping() -> None:
        connection = await asyncpg.connect(POSTGRES_DSN, timeout=5)
        await connection.close()

    try:
        asyncio.run(ping())
    except OSError as error:
        pytest.exit(
            f"PostgreSQL is not reachable at {POSTGRES_DSN} ({error}). "
            "Run `mise run test`, or `mise run db:start` first.",
            returncode=1,
        )

    return POSTGRES_DSN


def _database_dsns(dsn: str, database: str) -> tuple[str, str]:
    parts = urlsplit(dsn)
    maintenance = urlunsplit(parts._replace(path="/postgres"))
    target = urlunsplit(parts._replace(path=f"/{database}"))

    return maintenance, target


@dataclass(frozen=True)
class PortalDatabase:
    pool: asyncpg.Pool
    dsn: str


@asynccontextmanager
async def empty_database(
    cluster: str,
    label: str,
) -> AsyncIterator[PortalDatabase]:
    """A database of its own, created and dropped, with no schema applied."""
    prefix = label[:20].replace("[", "_").replace("]", "")
    database = f"portal_{prefix}_{uuid4().hex}".lower()
    maintenance_dsn, test_dsn = _database_dsns(cluster, database)

    maintenance = await asyncpg.connect(maintenance_dsn)

    try:
        await maintenance.execute(f'CREATE DATABASE "{database}"')
    finally:
        await maintenance.close()

    try:
        pool = await asyncpg.create_pool(test_dsn, min_size=1, max_size=12)

        try:
            yield PortalDatabase(pool, test_dsn)
        finally:
            await pool.close()
    finally:
        maintenance = await asyncpg.connect(maintenance_dsn)

        try:
            await maintenance.execute(
                f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'
            )
        finally:
            await maintenance.close()


@pytest.fixture
async def unmigrated_db(
    request: pytest.FixtureRequest,
    portal_cluster: str,
) -> AsyncIterator[PortalDatabase]:
    async with empty_database(portal_cluster, request.node.name) as database:
        yield database


@pytest.fixture
async def portal_db(
    request: pytest.FixtureRequest,
    portal_cluster: str,
) -> AsyncIterator[PortalDatabase]:
    async with empty_database(portal_cluster, request.node.name) as database:
        await apply_migrations(database.pool)
        yield database


@pytest.fixture
def pool(portal_db: PortalDatabase) -> asyncpg.Pool:
    return portal_db.pool


@pytest.fixture
def auth_repository(portal_db: PortalDatabase) -> PostgresAuthRepository:
    return PostgresAuthRepository(portal_db.pool)


@pytest.fixture
def team_repository(portal_db: PortalDatabase) -> PostgresTeamRepository:
    return PostgresTeamRepository(portal_db.pool)


@pytest.fixture
def credential_repository(
    portal_db: PortalDatabase,
) -> PostgresCredentialRepository:
    return PostgresCredentialRepository(portal_db.pool)


@pytest.fixture
def job_repository(portal_db: PortalDatabase) -> PostgresJobRepository:
    return PostgresJobRepository(portal_db.pool)


@pytest.fixture
def entry_repository(portal_db: PortalDatabase) -> PostgresEntryRepository:
    return PostgresEntryRepository(portal_db.pool)


@pytest.fixture
def audit_repository(portal_db: PortalDatabase) -> PostgresAuditLog:
    return PostgresAuditLog(portal_db.pool)


@pytest.fixture
def search_log_repository(portal_db: PortalDatabase) -> PostgresSearchLogRepository:
    return PostgresSearchLogRepository(portal_db.pool)


@pytest.fixture
def worker_registry(portal_db: PortalDatabase) -> PostgresWorkerRegistry:
    return PostgresWorkerRegistry(portal_db.pool)


@pytest.fixture
def service(
    team_repository: PostgresTeamRepository,
    credential_repository: PostgresCredentialRepository,
    job_repository: PostgresJobRepository,
    entry_repository: PostgresEntryRepository,
    search_log_repository: PostgresSearchLogRepository,
    worker_registry: PostgresWorkerRegistry,
) -> PortalService:
    return PortalService(
        team_repository,
        credential_repository,
        job_repository,
        entry_repository,
        search_log_repository,
        worker_registry,
    )


@pytest.fixture(scope="session")
def master_key_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A real key file, so tests load the keyring the way a deployment does."""
    path = tmp_path_factory.mktemp("keys") / "master.key"
    path.write_text(f"{MASTER_KEY_VERSION} {MASTER_KEY}\n", encoding="utf-8")

    return path


@pytest.fixture
def protector() -> EnvelopeProtector:
    return EnvelopeProtector(KEYRING)


@pytest.fixture
def store(pool: asyncpg.Pool) -> EphemeralStore:
    return EphemeralStore(pool)


@pytest.fixture
def sessions(
    store: EphemeralStore,
    auth_repository: PostgresAuthRepository,
) -> BrowserSessions:
    return BrowserSessions(store, auth_repository)


@pytest.fixture
def provisioning(
    auth_repository: PostgresAuthRepository,
    team_repository: PostgresTeamRepository,
    credential_repository: PostgresCredentialRepository,
    protector: EnvelopeProtector,
    audit_repository: PostgresAuditLog,
    store: EphemeralStore,
) -> ProvisioningService:
    return ProvisioningService(
        auth_repository,
        team_repository,
        credential_repository,
        protector,
        audit_repository,
        "testserver.local",
        public_origin=ORIGIN,
        setup_tokens=OneTimeTokens(store),
        mailer=ConsoleMailer(),
    )


@pytest.fixture(scope="session", autouse=True)
async def _object_storage_bucket() -> None:
    session = aioboto3.Session()

    async with session.client(
        "s3",
        endpoint_url=OBJECT_STORAGE_ENDPOINT,
        aws_access_key_id=OBJECT_STORAGE_ACCESS_KEY,
        aws_secret_access_key=OBJECT_STORAGE_SECRET_KEY,
        region_name="us-east-1",
    ) as client:
        try:
            await client.create_bucket(Bucket=OBJECT_STORAGE_BUCKET)
        except ClientError as error:
            if error.response["Error"]["Code"] != "BucketAlreadyOwnedByYou":
                raise


@pytest.fixture
def settings(
    portal_db: PortalDatabase,
    master_key_file: Path,
) -> PortalSettings:
    # No Turnstile keys, so open_human_check() returns the disabled check. That
    # is the same branch a developer's laptop takes, and validate() refuses it
    # in production.
    return PortalSettings(
        database_dsn=portal_db.dsn,
        public_origin=ORIGIN,
        master_key_file=master_key_file,
        object_storage_endpoint=OBJECT_STORAGE_ENDPOINT,
        object_storage_bucket=OBJECT_STORAGE_BUCKET,
        object_storage_access_key=OBJECT_STORAGE_ACCESS_KEY,
        object_storage_secret_key=OBJECT_STORAGE_SECRET_KEY,
    )


@pytest.fixture
def app(settings: PortalSettings) -> Litestar:
    return create_web_app(settings)


@pytest.fixture
def worker_api(settings: PortalSettings) -> Litestar:
    return create_worker_api(settings)


@pytest.fixture
async def worker_client(worker_api: Litestar) -> AsyncIterator[AsyncTestClient]:
    async with AsyncTestClient(
        app=worker_api,
        base_url="https://worker-api.tailnet.test",
    ) as http_client:
        yield http_client


@pytest.fixture
async def client(app: Litestar) -> AsyncIterator[AsyncTestClient]:
    async with AsyncTestClient(app=app, base_url=ORIGIN) as http_client:
        yield http_client


class SeededTeam(NamedTuple):
    actor_id: UUID
    team_id: UUID
    credential_id: UUID


async def seed_team(
    pool: asyncpg.Pool,
    *,
    config: ProtectedSecret = UNREADABLE_SECRET,
    password_hash: str = "x",
) -> SeededTeam:
    actor_id, team_id, credential_id = uuid4(), uuid4(), uuid4()
    credential_root_id = uuid4()

    async with pool.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO portal_users (id, email, password_hash)
            VALUES ($1, $2, $3)
            """,
            actor_id,
            f"{actor_id.hex}@example.test",
            password_hash,
        )

        await connection.execute(
            """
            INSERT INTO portal_teams (id, slug, name, created_by)
            VALUES ($1, $2, 'Equipo', $3)
            """,
            team_id,
            f"equipo-{team_id.hex[:10]}",
            actor_id,
        )

        await connection.execute(
            """
            INSERT INTO portal_team_memberships (team_id, user_id, role)
            VALUES ($1, $2, 'team_leader')
            """,
            team_id,
            actor_id,
        )

        await connection.execute(
            """
            INSERT INTO portal_team_proxy_credentials
                (id, team_id, label, created_by)
            VALUES ($1, $2, 'Proxy Perú', $3)
            """,
            credential_root_id,
            team_id,
            actor_id,
        )

        await connection.execute(
            """
            INSERT INTO portal_team_proxy_credential_versions
                (id, credential_id, team_id, version, provider, config_ciphertext,
                 wrapped_data_key, master_key_version, lifecycle, is_active,
                 created_by)
            VALUES ($1, $2, $3, 1, 'geonode', $4, $5, $6, 'active', true, $7)
            """,
            credential_id,
            credential_root_id,
            team_id,
            config.ciphertext,
            config.wrapped_data_key,
            config.master_key_version,
            actor_id,
        )

    return SeededTeam(actor_id, team_id, credential_id)


async def seed_user(pool: asyncpg.Pool, *, email: str | None = None) -> UUID:
    user_id = uuid4()

    await pool.execute(
        "INSERT INTO portal_users (id, email, password_hash) VALUES ($1, $2, 'x')",
        user_id,
        email or f"{user_id.hex}@example.test",
    )

    return user_id


def submit_command(
    team: SeededTeam,
    input_object_id: UUID,
    *,
    actor_id: UUID | None = None,
    value: str = "10412345678",
    sources: tuple[str, ...] = ("osiptel",),
) -> SubmitJob:
    return SubmitJob(
        actor_id=actor_id or team.actor_id,
        team_id=team.team_id,
        credential_version_id=team.credential_id,
        input_object_id=input_object_id,
        filename="entrada.csv",
        sources=sources,
        lines=(InputLine(1, value),),
    )


async def object_reference(
    pool: asyncpg.Pool,
    team_id: UUID,
    key: str,
) -> UUID:
    reference_id = uuid4()

    await pool.execute(
        """
        INSERT INTO portal_object_references
            (id, team_id, provider, container, object_key, sha256, size_bytes,
             content_type)
        VALUES ($1, $2, 'prueba', 'contenedor', $3, repeat('a', 64), 1, 'text/csv')
        """,
        reference_id,
        team_id,
        key,
    )

    return reference_id


async def publish_claimed(
    pool: asyncpg.Pool,
    job_repository: PostgresJobRepository,
    claimed: ClaimedWork,
    *,
    status: str = "ok",
    columns: tuple[str, ...] = ("documento",),
    rows: tuple[tuple[object, ...], ...] | None = None,
    error_code: str | None = None,
    worker_id: str = "trabajador",
    provider: str = "geonode",
    lane_index: int = 0,
    attempts: tuple[AttemptRecord, ...] = (),
) -> bool:
    """Publish a claimed item with a real entry payload, the shape every
    worker-api caller must supply since publish() started upserting
    portal_entries. rows defaults to the claimed document itself so a test
    asserting search/entry content has something to match on."""
    team_id = await job_repository.item_team(claimed.item_id)
    assert team_id is not None

    return await job_repository.publish(
        claimed.item_id,
        worker_id,
        claimed.lease_fence,
        document=claimed.document,
        source=claimed.source,
        provider=provider,
        status=status,
        columns=columns,
        rows=rows if rows is not None else ((claimed.document,),),
        error_code=error_code,
        result_object_id=await object_reference(
            pool,
            team_id,
            f"salida/{claimed.item_id}.json",
        ),
        lane_index=lane_index,
        attempts=attempts,
    )


def csrf_token(html: str) -> str:
    found = re.search(r'name="csrf_token" value="([^"]+)"', html)

    assert found is not None

    return found.group(1)


def hidden_value(html: str, name: str) -> str:
    """A single hidden <input>'s value, e.g. JobReview's input_object_id.

    djlint wraps a tag with several attributes onto multiple lines and
    reorders them, so this matches name= and value= independently within
    one <input ...> tag rather than assuming they are adjacent.
    """
    found = re.search(
        rf'<input\b(?=[^>]*\bname="{re.escape(name)}")'
        rf'(?=[^>]*\bvalue="([^"]*)")[^>]*>',
        html,
    )

    assert found is not None

    return found.group(1)


def login(
    client: TestClient,
    email: str,
    password: str = PASSWORD,
    totp_secret: str | None = None,
) -> httpx.Response:
    """Sign in, completing the second factor when the account asks for one."""
    page = client.get("/login")

    response = client.post(
        "/login",
        data={
            "email": email,
            "password": password,
            "csrf_token": csrf_token(page.text),
        },
        headers={"Origin": ORIGIN},
        follow_redirects=False,
    )

    if response.headers.get("location") != "/login/mfa":
        return response

    return submit_mfa_code(client, pyotp.TOTP(totp_secret or TOTP_SECRET).now())


def submit_mfa_code(client: TestClient, code: str) -> httpx.Response:
    return client.post(
        "/login/mfa",
        data={"code": code},
        headers={"Origin": ORIGIN},
        follow_redirects=False,
    )


def session_csrf(client: TestClient) -> str:
    return csrf_token(client.get("/").text)


def sync_client(app: Litestar) -> TestClient:
    return TestClient(app, base_url=ORIGIN)


async def enroll_worker(pool: asyncpg.Pool) -> dict[str, str]:
    """Register a worker and return the headers it authenticates with."""
    credential = new_worker_credential()

    await PostgresWorkerRegistry(pool).issue(WORKER_ID, credential, WORKER_HOSTNAME)

    return {
        "Authorization": f"Bearer {credential}",
        "X-Portal-Worker": WORKER_ID,
    }


@dataclass(frozen=True)
class Experience:
    admin_id: UUID
    leader_id: UUID
    member_id: UUID
    team_id: UUID
    credential_id: UUID


async def seed_site_admin(pool: asyncpg.Pool, email: str) -> UUID:
    """Promote a new account, which the schema only allows once MFA exists."""
    auth = PostgresAuthRepository(pool)
    protector = EnvelopeProtector(KEYRING)
    user = await auth.create_account(email, hash_password(PASSWORD))

    await auth.enable_totp(
        user.id,
        protector.protect(TOTP_SECRET.encode("utf-8")),
        (token_hash(RECOVERY_CODE),),
        promote_to_site_admin=True,
    )

    return user.id


async def build_experience(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
) -> Experience:
    hashed = hash_password(PASSWORD)
    team = await seed_team(pool, password_hash=hashed)
    other = await seed_team(pool, password_hash=hashed)
    admin_id = await seed_site_admin(pool, "admin@osiptel.test")
    member_id = await seed_user(pool, email="miembro@osiptel.test")

    await pool.execute(
        """
        UPDATE portal_users
           SET password_hash = $1,
               email = CASE id
                           WHEN $2 THEN 'lider@osiptel.test'
                           WHEN $3 THEN 'otro@osiptel.test'
                           ELSE email
                       END
         WHERE id = ANY($4::uuid[])
        """,
        hashed,
        team.actor_id,
        other.actor_id,
        [member_id, team.actor_id, other.actor_id],
    )

    await team_repository.add_member(
        team.team_id,
        member_id,
        TeamRole.TEAM_MEMBER,
    )

    return Experience(
        admin_id=admin_id,
        leader_id=team.actor_id,
        member_id=member_id,
        team_id=team.team_id,
        credential_id=team.credential_id,
    )


def submit_job(
    client: TestClient,
    team_id: UUID,
    credential_id: UUID,
    documents: str,
    *,
    filename: str = "registros.csv",
) -> UUID:
    response = client.post(
        f"/teams/{team_id}/jobs",
        data={
            "credential_version_id": str(credential_id),
            "sources": "osiptel",
            "csrf_token": session_csrf(client),
        },
        files={
            "input_file": (
                filename,
                documents.encode(),
                "text/csv",
            )
        },
        headers={"Origin": ORIGIN},
        follow_redirects=False,
    )

    assert response.status_code == 303

    return UUID(response.headers["location"].rsplit("/", 1)[1])


def submit_csv(
    client: TestClient,
    team_id: UUID,
    credential_id: UUID,
) -> UUID:
    return submit_job(
        client,
        team_id,
        credential_id,
        "10412345678\n10412345679\n",
        filename="barranca.csv",
    )
