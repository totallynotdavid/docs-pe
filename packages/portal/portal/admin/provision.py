from __future__ import annotations

import argparse
import asyncio
import os

from typing import TYPE_CHECKING

import asyncpg

from core.proxy.registry import PROVIDERS, spec_for

from portal.application.provisioning import ProvisioningService
from portal.application.sessions import OneTimeTokens
from portal.credentials.masterkey import MasterKeyring
from portal.credentials.secrets import EnvelopeProtector
from portal.domain.models import CredentialState, RequestTrace
from portal.ephemeral import EphemeralStore
from portal.notify.mailer import open_mailer
from portal.repository.audit import PostgresAuditLog
from portal.repository.auth import PostgresAuthRepository
from portal.repository.credentials import PostgresCredentialRepository
from portal.repository.teams import PostgresTeamRepository
from portal.security import hash_password
from portal.settings import PortalSettings


if TYPE_CHECKING:
    from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="portal-admin provision",
        description="Idempotently provision the portal's initial installation.",
    )
    parser.add_argument("--admin-email", required=True)
    parser.add_argument("--admin-password-env", required=True)
    parser.add_argument("--team-name", required=True)
    parser.add_argument("--team-slug", required=True)
    parser.add_argument("--proxy-provider", choices=sorted(PROVIDERS))
    parser.add_argument("--proxy-label", default="Principal")
    return parser


def _environment_value(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _proxy_values(provider: str) -> dict[str, str]:
    spec = spec_for(provider)
    values: dict[str, str] = {}

    for field in spec.fields:
        variable = f"PORTAL_PROVISION_{provider}_{field.name}".upper()
        values[field.name] = (
            _environment_value(variable)
            if field.secret
            else os.environ.get(variable, field.default)
        )

    return values


def _print_setup_status(email: str, *, needs_setup: bool) -> None:
    """Never prints a secret: nothing generated here is shown to whoever runs
    this command, only to the account owner, in their own browser."""
    if needs_setup:
        print(
            f"Second factor: pending. Sign in as {email} and open "
            "/security/setup to finish enrollment."
        )
    else:
        print("Second factor: already enrolled (unchanged)")


async def provision(args: argparse.Namespace) -> None:
    settings = PortalSettings.from_environment()
    settings.validate()

    password = _environment_value(args.admin_password_env)
    password_hash = hash_password(password)

    pool = await asyncpg.create_pool(settings.database_dsn)
    mailer = open_mailer(settings)

    try:
        credential_repo = PostgresCredentialRepository(pool)

        service = ProvisioningService(
            PostgresAuthRepository(pool),
            PostgresTeamRepository(pool),
            credential_repo,
            EnvelopeProtector(MasterKeyring.from_file(settings.master_key_file)),
            PostgresAuditLog(pool),
            settings.hostname,
            public_origin=settings.public_origin,
            setup_tokens=OneTimeTokens(EphemeralStore(pool)),
            mailer=mailer,
        )

        administrator, needs_setup = await service.ensure_site_admin(
            args.admin_email,
            password_hash,
        )

        print(f"Administrator: {administrator.email} (ready)")
        _print_setup_status(administrator.email, needs_setup=needs_setup)

        if needs_setup:
            print("Team and proxy setup deferred until enrollment completes.")
            return

        first_team = await service.ensure_first_team(
            administrator.id,
            name=args.team_name,
            slug=args.team_slug,
        )

        print(
            f"Team: {first_team.team.name} · {first_team.team.slug} "
            f"({'created' if first_team.created else 'verified'})"
        )

        provider = args.proxy_provider
        if provider:
            label = args.proxy_label.strip()

            credential = next(
                (
                    credential
                    for credential in await credential_repo.credentials_for_team(
                        first_team.team.id
                    )
                    if credential.label == label
                    and credential.state is CredentialState.ACTIVE
                ),
                None,
            )

            if credential is None:
                credential = await service.configure_proxy(
                    administrator.id,
                    team_id=first_team.team.id,
                    label=label,
                    provider=provider,
                    values=_proxy_values(provider),
                    trace=RequestTrace(),
                )
                print(f"Proxy: {credential.label} · {provider} (validated and active)")
            else:
                print(f"Proxy: {credential.label} · {provider} (verified)")
    finally:
        await mailer.aclose()
        await pool.close()


def run(argv: Sequence[str]) -> None:
    args = build_parser().parse_args(argv)

    try:
        asyncio.run(provision(args))
    except Exception as error:
        raise SystemExit(f"Provisioning did not complete: {error}") from error
