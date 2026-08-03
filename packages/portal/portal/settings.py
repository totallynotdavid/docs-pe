from __future__ import annotations

import os

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True)
class PortalSettings:
    database_dsn: str
    environment: str = "development"
    public_origin: str = "http://testserver"
    cookie_secure: bool = False
    tls_terminated_upstream: bool = False
    object_root: Path = Path(".data/objects")

    @classmethod
    def from_environment(cls) -> PortalSettings:
        environment = os.environ.get("PORTAL_ENVIRONMENT", "development").lower()
        origin = os.environ.get("PORTAL_PUBLIC_ORIGIN", "")
        secure = os.environ.get("PORTAL_COOKIE_SECURE", "").lower()
        tls_terminated_upstream = (
            os.environ.get("PORTAL_TLS_TERMINATED_UPSTREAM", "").lower() == "true"
        )
        return cls(
            database_dsn=os.environ.get("PORTAL_DATABASE_DSN", ""),
            environment=environment,
            public_origin=origin
            or ("" if environment == "production" else "http://testserver"),
            cookie_secure=secure == "true" if secure else environment == "production",
            tls_terminated_upstream=tls_terminated_upstream,
            object_root=Path(os.environ.get("PORTAL_OBJECT_ROOT", ".data/objects")),
        )

    def validate(self) -> None:
        # The portal is PostgreSQL-only in every environment: there is one
        # repository, so a missing DSN cannot degrade into something else.
        if not self.database_dsn:
            msg = "PORTAL_DATABASE_DSN is required"
            raise RuntimeError(msg)
        if not self.is_production:
            return
        if not self.cookie_secure or urlparse(self.public_origin).scheme != "https":
            msg = "production requires HTTPS and Secure cookies"
            raise RuntimeError(msg)

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def session_cookie(self) -> str:
        return "__Host-portal-id" if self.cookie_secure else "portal-id"
