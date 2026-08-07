from __future__ import annotations

import os

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()

    if not value:
        msg = f"{name} is required"
        raise RuntimeError(msg)

    return value


def _required_bool(name: str) -> bool:
    value = _required(name).lower()

    if value not in {"true", "false"}:
        msg = f"{name} must be 'true' or 'false'"
        raise RuntimeError(msg)

    return value == "true"


@dataclass(frozen=True)
class PortalSettings:
    database_dsn: str
    worker_bootstrap_token: str
    environment: str = "development"
    public_origin: str = "http://testserver.local"
    cookie_secure: bool = False
    tls_terminated_upstream: bool = False
    object_root: Path = Path(".data/objects")

    @classmethod
    def from_environment(cls) -> PortalSettings:
        return cls(
            database_dsn=_required("PORTAL_DATABASE_DSN"),
            worker_bootstrap_token=_required("PORTAL_WORKER_BOOTSTRAP_TOKEN"),
            environment=_required("PORTAL_ENVIRONMENT").lower(),
            public_origin=_required("PORTAL_PUBLIC_ORIGIN"),
            cookie_secure=_required_bool("PORTAL_COOKIE_SECURE"),
            tls_terminated_upstream=_required_bool(
                "PORTAL_TLS_TERMINATED_UPSTREAM",
            ),
            object_root=Path(
                os.environ.get("PORTAL_OBJECT_ROOT", ".data/objects"),
            ),
        )

    def validate(self) -> None:
        if not self.database_dsn:
            msg = "PORTAL_DATABASE_DSN is required"
            raise RuntimeError(msg)

        if not self.worker_bootstrap_token:
            msg = "PORTAL_WORKER_BOOTSTRAP_TOKEN is required"
            raise RuntimeError(msg)

        if not self.is_production:
            return

        origin = urlparse(self.public_origin)

        if not self.cookie_secure or origin.scheme != "https":
            msg = "production requires HTTPS and Secure cookies"
            raise RuntimeError(msg)

        if not origin.hostname:
            msg = "PORTAL_PUBLIC_ORIGIN must include a hostname"
            raise RuntimeError(msg)

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def session_cookie(self) -> str:
        return "__Host-portal-id" if self.cookie_secure else "portal-id"
