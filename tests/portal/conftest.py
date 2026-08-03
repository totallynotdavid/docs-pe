from __future__ import annotations

import asyncio
import base64
import os
import re

from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest

from fastapi.testclient import TestClient
from portal.application.provisioning import ProvisioningService
from portal.application.service import PortalService
from portal.credentials.secrets import AesGcmSecretProtector
from portal.domain.models import InputLine, SubmitJob, TeamRole
from portal.migrations import apply_migrations
from portal.repository.auth import PostgresAuthRepository
from portal.repository.credentials import PostgresCredentialRepository
from portal.repository.jobs import PostgresJobRepository
from portal.repository.teams import PostgresTeamRepository
from portal.security import hash_password
from portal.settings import PortalSettings
from portal.web.app import create_app


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from fastapi import FastAPI


POSTGRES_DSN = (
    os.environ.get("PORTAL_TEST_DSN") or "postgresql://postgres@127.0.0.1:5432/postgres"
)
SECRET_KEY = base64.urlsafe_b64encode(b"c" * 32).decode("ascii")

ORIGIN = "http://testserver"
PASSWORD = "una-clave-larga-y-segura"


@pytest.fixture(scope="session")
def portal_cluster() -> str:
    """Require PostgreSQL only when a test requests this fixture."""

    async def ping() -> None:
        connection = await asyncpg.connect(POSTGRES_DSN, timeout=5)
        await connection.close()

    try:
        asyncio.run(ping())
    except OSError as error:
        pytest.exit(
            f"PostgreSQL is not reachable at {POSTGRES_DSN} ({error}). "
            "Run `mise run test`, or `mise run portal:db:start` first.",
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


@pytest.fixture
async def portal_db(
    request: pytest.FixtureRequest,
    portal_cluster: str,
) -> AsyncIterator[PortalDatabase]:
    prefix = request.node.name[:20].replace("[", "_").replace("]", "")
    database = f"portal_{prefix}_{uuid4().hex}".lower()
    maintenance_dsn, test_dsn = _database_dsns(portal_cluster, database)

    maintenance = await asyncpg.connect(maintenance_dsn)

    try:
        await maintenance.execute(f'CREATE DATABASE "{database}"')
    finally:
        await maintenance.close()

    try:
        pool = await asyncpg.create_pool(test_dsn, min_size=1, max_size=12)

        try:
            await apply_migrations(pool)
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
def pool(portal_db: PortalDatabase) -> asyncpg.Pool:
    return portal_db.pool


@pytest.fixture
def auth_repository(portal_db: PortalDatabase) -> PostgresAuthRepository:
    return PostgresAuthRepository(portal_db.pool)


@pytest.fixture
def team_repository(portal_db: PortalDatabase) -> PostgresTeamRepository:
    return PostgresTeamRepository(portal_db.pool)


@pytest.fixture
def credential_repository(portal_db: PortalDatabase) -> PostgresCredentialRepository:
    return PostgresCredentialRepository(portal_db.pool)


@pytest.fixture
def job_repository(portal_db: PortalDatabase) -> PostgresJobRepository:
    return PostgresJobRepository(portal_db.pool)


@pytest.fixture
def service(
    auth_repository: PostgresAuthRepository,
    team_repository: PostgresTeamRepository,
    credential_repository: PostgresCredentialRepository,
    job_repository: PostgresJobRepository,
) -> PortalService:
    return PortalService(
        auth_repository, team_repository, credential_repository, job_repository
    )


@pytest.fixture
def protector() -> AesGcmSecretProtector:
    return AesGcmSecretProtector(SECRET_KEY)


@pytest.fixture
def provisioning(
    auth_repository: PostgresAuthRepository,
    team_repository: PostgresTeamRepository,
    credential_repository: PostgresCredentialRepository,
    protector: AesGcmSecretProtector,
) -> ProvisioningService:
    return ProvisioningService(
        auth_repository, team_repository, credential_repository, protector
    )


@pytest.fixture
def app(
    portal_db: PortalDatabase,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[FastAPI]:
    monkeypatch.setenv("PORTAL_SECRET_PROTECTION_KEY", SECRET_KEY)

    yield create_app(
        PortalSettings(
            database_dsn=portal_db.dsn,
            object_root=tmp_path_factory.mktemp("objects"),
        )
    )


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Run the app and asyncpg pool on the same event loop."""

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=ORIGIN,
        ) as http_client,
    ):
        yield http_client


class SeededTeam(NamedTuple):
    actor_id: UUID
    team_id: UUID
    credential_id: UUID


async def seed_team(
    pool: asyncpg.Pool,
    *,
    ciphertext: bytes = b"cifrado",
    password_hash: str = "x",
    is_site_admin: bool = False,
) -> SeededTeam:
    actor_id, team_id, credential_id = uuid4(), uuid4(), uuid4()
    credential_root_id = uuid4()

    async with pool.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO portal_users (id, email, password_hash, is_site_admin)
            VALUES ($1, $2, $3, $4)
            """,
            actor_id,
            f"{actor_id.hex}@example.test",
            password_hash,
            is_site_admin,
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
                 key_id, lifecycle, is_active, created_by)
            VALUES ($1, $2, $3, 1, 'geonode', $4, 'clave-prueba', 'active', true, $5)
            """,
            credential_id,
            credential_root_id,
            team_id,
            ciphertext,
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


def csrf_token(html: str) -> str:
    found = re.search(r'name="csrf_token" value="([^"]+)"', html)

    assert found is not None

    return found.group(1)


def login(
    client: TestClient,
    email: str,
    password: str = PASSWORD,
) -> httpx.Response:
    page = client.get("/login")

    return client.post(
        "/login",
        data={
            "email": email,
            "password": password,
            "csrf_token": csrf_token(page.text),
        },
        headers={"Origin": ORIGIN},
        follow_redirects=False,
    )


def session_csrf(client: TestClient) -> str:
    return csrf_token(client.get("/").text)


def sync_client(app: FastAPI) -> TestClient:
    return TestClient(app)


@dataclass(frozen=True)
class Experience:
    admin_id: UUID
    leader_id: UUID
    member_id: UUID
    team_id: UUID
    credential_id: UUID


async def build_experience(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
) -> Experience:
    hashed = hash_password(PASSWORD)
    team = await seed_team(pool, password_hash=hashed)
    other = await seed_team(pool, password_hash=hashed)
    admin_id = await seed_user(pool, email="admin@osiptel.test")
    member_id = await seed_user(pool, email="miembro@osiptel.test")

    await pool.execute(
        """
        UPDATE portal_users
           SET password_hash = $2,
               is_site_admin = (id = $1),
               email = CASE id
                           WHEN $3 THEN 'lider@osiptel.test'
                           WHEN $4 THEN 'otro@osiptel.test'
                           ELSE email
                       END
         WHERE id = ANY($5::uuid[])
        """,
        admin_id,
        hashed,
        team.actor_id,
        other.actor_id,
        [admin_id, member_id, team.actor_id, other.actor_id],
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
        f"/equipos/{team_id}/procesos",
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
