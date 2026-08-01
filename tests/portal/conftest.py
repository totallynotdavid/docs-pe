from __future__ import annotations

import base64
import os

from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest

from portal.application.provisioning import ProvisioningService
from portal.application.service import PortalService
from portal.credentials.secrets import AesGcmSecretProtector
from portal.domain.models import InputLine, SubmitJob
from portal.migrations import apply_migrations
from portal.repository.postgres import PostgresPortalRepository
from portal.settings import PortalSettings
from portal.web.app import create_app


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from fastapi import FastAPI


POSTGRES_DSN = os.environ.get("PORTAL_TEST_DSN", "")
SECRET_KEY = base64.urlsafe_b64encode(b"c" * 32).decode("ascii")


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip the whole package at once when no database is configured."""
    del config
    if POSTGRES_DSN:
        return
    skip = pytest.mark.skip(reason="set PORTAL_TEST_DSN to run the portal suite")
    for item in items:
        if "tests/portal/" in item.nodeid.replace(os.sep, "/"):
            item.add_marker(skip)


def _database_dsns(dsn: str, database: str) -> tuple[str, str]:
    parts = urlsplit(dsn)
    maintenance = urlunsplit(parts._replace(path="/postgres"))
    target = urlunsplit(parts._replace(path=f"/{database}"))
    return maintenance, target


@dataclass(frozen=True)
class PortalDatabase:
    """A migrated database plus the DSN an app under test should open."""

    pool: asyncpg.Pool
    dsn: str


@pytest.fixture
async def portal_db(request: pytest.FixtureRequest) -> AsyncIterator[PortalDatabase]:
    prefix = request.node.name[:20].replace("[", "_").replace("]", "")
    database = f"portal_{prefix}_{uuid4().hex}".lower()
    maintenance_dsn, test_dsn = _database_dsns(POSTGRES_DSN, database)
    maintenance = await asyncpg.connect(maintenance_dsn)
    await maintenance.execute(f'CREATE DATABASE "{database}"')
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
        await maintenance.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        await maintenance.close()


@pytest.fixture
def pool(portal_db: PortalDatabase) -> asyncpg.Pool:
    return portal_db.pool


@pytest.fixture
def repository(portal_db: PortalDatabase) -> PostgresPortalRepository:
    return PostgresPortalRepository(portal_db.pool)


@pytest.fixture
def service(repository: PostgresPortalRepository) -> PortalService:
    return PortalService(repository)


@pytest.fixture
def protector() -> AesGcmSecretProtector:
    return AesGcmSecretProtector(SECRET_KEY)


@pytest.fixture
def provisioning(
    repository: PostgresPortalRepository, protector: AesGcmSecretProtector
) -> ProvisioningService:
    return ProvisioningService(repository, protector)


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
    """Drive the app on this test's own event loop.

    `TestClient` runs the app in a second loop, and an asyncpg pool belongs to the
    loop that created it, so the two cannot share one database.
    """
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as http_client,
    ):
        yield http_client


class SeededTeam(NamedTuple):
    """Named so a test can read one field, and a tuple so it can unpack all three."""

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
    """Insert a leader, their team, and one active proxy credential."""
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
            INSERT INTO portal_team_proxy_credentials (id, team_id, label, created_by)
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


async def object_reference(pool: asyncpg.Pool, team_id: UUID, key: str) -> UUID:
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
