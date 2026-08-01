from __future__ import annotations

import argparse
import asyncio
import os

from portal.application.provisioning import ProvisioningService
from portal.credentials.secrets import (
    DevelopmentAesGcmSecretProtector,
    SecretProtector,
    UnavailableSecretProtector,
)
from portal.domain.models import CredentialState, ProxyProvider, TeamRole
from portal.migrations import apply_migrations
from portal.repository.postgres import PostgresPortalRepository
from portal.settings import PortalSettings
from portal.web.security import hash_password


def _arguments() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Provisiona de forma idempotente la instalación inicial del portal."
    )
    parser.add_argument("--admin-email", required=True)
    parser.add_argument("--admin-password-env", required=True)
    parser.add_argument("--team-name", required=True)
    parser.add_argument("--team-slug", required=True)
    parser.add_argument(
        "--proxy-provider", choices=[provider.value for provider in ProxyProvider]
    )
    parser.add_argument("--proxy-label", default="Principal")
    return parser


def _environment_value(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        msg = f"la variable {name} es obligatoria"
        raise RuntimeError(msg)
    return value


def _proxy_values(provider: ProxyProvider) -> dict[str, str]:
    if provider is ProxyProvider.GEONODE:
        return {
            "username": _environment_value("PORTAL_PROVISION_GEONODE_USERNAME"),
            "password": _environment_value("PORTAL_PROVISION_GEONODE_PASSWORD"),
            "gateway": os.environ.get("PORTAL_PROVISION_GEONODE_GATEWAY", "fr"),
            "proxy_type": os.environ.get(
                "PORTAL_PROVISION_GEONODE_TYPE", "residential"
            ),
            "country": os.environ.get("PORTAL_PROVISION_GEONODE_COUNTRY", "PE"),
            "state": os.environ.get("PORTAL_PROVISION_GEONODE_STATE", ""),
            "city": os.environ.get("PORTAL_PROVISION_GEONODE_CITY", ""),
            "asn": os.environ.get("PORTAL_PROVISION_GEONODE_ASN", ""),
            "lifetime_minutes": os.environ.get(
                "PORTAL_PROVISION_GEONODE_LIFETIME_MINUTES", "10"
            ),
        }
    return {
        "username": _environment_value("PORTAL_PROVISION_DATAIMPULSE_USERNAME"),
        "password": _environment_value("PORTAL_PROVISION_DATAIMPULSE_PASSWORD"),
        "country": os.environ.get("PORTAL_PROVISION_DATAIMPULSE_COUNTRY", "pe"),
        "session_minutes": os.environ.get(
            "PORTAL_PROVISION_DATAIMPULSE_SESSION_MINUTES", "3"
        ),
    }


async def provision(args: argparse.Namespace) -> None:
    settings = PortalSettings.from_environment()
    if not settings.database_dsn:
        msg = "PORTAL_DATABASE_DSN es obligatorio para provisionar"
        raise RuntimeError(msg)
    password = _environment_value(args.admin_password_env)
    import asyncpg

    pool = await asyncpg.create_pool(settings.database_dsn)
    try:
        await apply_migrations(pool)
        repository = PostgresPortalRepository(pool)
        administrator = await repository.provision_site_admin(
            args.admin_email, hash_password(password)
        )
        protector: SecretProtector = UnavailableSecretProtector()
        protector = DevelopmentAesGcmSecretProtector.from_environment() or protector
        service = ProvisioningService(repository, protector)
        team_count, initial_team_id = await repository.installation_status()
        if team_count == 0:
            team = await service.create_first_team(
                administrator.id, name=args.team_name, slug=args.team_slug
            )
            team_status = "creado"
        else:
            existing_team = await repository.team_by_slug(
                args.team_slug.strip().lower()
            )
            if existing_team is None or initial_team_id != existing_team.id:
                msg = "la instalación ya tiene un equipo inicial diferente"
                raise RuntimeError(msg)
            team = existing_team
            await repository.add_member(team.id, administrator.id, TeamRole.TEAM_LEADER)
            team_status = "verificado"
        print(f"Administrador: {administrator.email} (listo)")
        print(f"Equipo: {team.name} · {team.slug} ({team_status})")
        if args.proxy_provider:
            provider = ProxyProvider(args.proxy_provider)
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
                print(
                    f"Proxy: {credential.label} · {provider.value} (validado y activo)"
                )
            else:
                print(f"Proxy: {active.label} · {provider.value} (verificado)")
    finally:
        await pool.close()


def main() -> None:
    args = _arguments().parse_args()
    try:
        asyncio.run(provision(args))
    except Exception as error:
        # Commands may name a missing environment variable but never print its value.
        raise SystemExit(f"Provisionamiento no completado: {error}") from error


if __name__ == "__main__":
    main()
