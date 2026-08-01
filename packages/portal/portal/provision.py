from __future__ import annotations

import argparse
import asyncio
import os

from fetch.proxy.registry import PROVIDERS, spec_for

from portal.application.provisioning import ProvisioningService
from portal.credentials.secrets import AesGcmSecretProtector
from portal.domain.models import CredentialState, TeamRole
from portal.migrations import apply_migrations
from portal.repository.postgres import PostgresPortalRepository
from portal.security import hash_password
from portal.settings import PortalSettings


def _arguments() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Idempotently provision the portal's initial installation."
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
        msg = f"{name} is required"
        raise RuntimeError(msg)
    return value


def _proxy_values(provider: str) -> dict[str, str]:
    """Read a provider's fields from `PORTAL_PROVISION_<PROVIDER>_<FIELD>`.

    Driven by the field schema, so provisioning a newly added vendor needs no
    change here.
    """
    spec = spec_for(provider)
    values: dict[str, str] = {}
    for field in spec.fields:
        variable = f"PORTAL_PROVISION_{provider}_{field.name}".upper()
        if field.secret:
            values[field.name] = _environment_value(variable)
        else:
            values[field.name] = os.environ.get(variable, field.default)
    return values


async def provision(args: argparse.Namespace) -> None:
    settings = PortalSettings.from_environment()
    settings.validate()
    password = _environment_value(args.admin_password_env)
    import asyncpg

    pool = await asyncpg.create_pool(settings.database_dsn)
    try:
        await apply_migrations(pool)
        repository = PostgresPortalRepository(pool)
        administrator = await repository.provision_site_admin(
            args.admin_email, hash_password(password)
        )
        service = ProvisioningService(
            repository, AesGcmSecretProtector.from_environment()
        )
        team_count, initial_team_id = await repository.installation_status()
        if team_count == 0:
            team = await service.create_first_team(
                administrator.id, name=args.team_name, slug=args.team_slug
            )
            team_status = "created"
        else:
            existing_team = await repository.team_by_slug(
                args.team_slug.strip().lower()
            )
            if existing_team is None or initial_team_id != existing_team.id:
                msg = "the installation already has a different initial team"
                raise RuntimeError(msg)
            team = existing_team
            await repository.add_member(team.id, administrator.id, TeamRole.TEAM_LEADER)
            team_status = "verified"
        print(f"Administrator: {administrator.email} (ready)")
        print(f"Team: {team.name} · {team.slug} ({team_status})")
        if args.proxy_provider:
            provider = str(args.proxy_provider)
            existing = await repository.credentials_for_team(team.id)
            active = next(
                (
                    credential
                    for credential in existing
                    if credential.label == args.proxy_label.strip()
                    and credential.state is CredentialState.ACTIVE
                ),
                None,
            )
            if active is None:
                credential = await service.configure_proxy(
                    administrator.id,
                    team_id=team.id,
                    label=args.proxy_label,
                    provider=provider,
                    values=_proxy_values(provider),
                )
                print(f"Proxy: {credential.label} · {provider} (validated and active)")
            else:
                print(f"Proxy: {active.label} · {provider} (verified)")
    finally:
        await pool.close()


def main() -> None:
    args = _arguments().parse_args()
    try:
        asyncio.run(provision(args))
    except Exception as error:
        # Commands may name a missing environment variable but never print its value.
        raise SystemExit(f"Provisioning did not complete: {error}") from error


if __name__ == "__main__":
    main()
