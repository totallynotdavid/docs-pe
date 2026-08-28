from __future__ import annotations

import os

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


# HTTPS, Secure cookies, and host checking are not gated on the environment:
# they follow PORTAL_PUBLIC_ORIGIN, which is the one place the deployment
# declares how it is reached. PORTAL_ENVIRONMENT gates only what makes a laptop
# workable: a plain-http origin and no Turnstile. validate() refuses both in
# production.


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()

    if not value:
        msg = f"{name} is required"
        raise RuntimeError(msg)

    return value


def _optional(name: str) -> str:
    return os.environ.get(name, "").strip()


def _required_bool(name: str) -> bool:
    value = _required(name).lower()

    if value not in {"true", "false"}:
        msg = f"{name} must be 'true' or 'false'"
        raise RuntimeError(msg)

    return value == "true"


def _required_choice(name: str, allowed: frozenset[str]) -> str:
    value = _required(name).lower()

    if value not in allowed:
        msg = f"{name} must be one of {', '.join(sorted(allowed))}"
        raise RuntimeError(msg)

    return value


ENVIRONMENTS = frozenset({"development", "production"})

DEFAULT_WORKER_API_PORT = 8443

# One uvicorn worker process fields all fleet claim/publish/heartbeat traffic
# regardless of host core count. 4 processes on a 6-core host leaves headroom
# for Postgres and other tenants on a shared box; raise per-host if the box is
# dedicated or has more cores.
DEFAULT_WORKER_API_WORKERS = 4


@dataclass(frozen=True)
class PortalSettings:
    database_dsn: str
    environment: str = "development"
    public_origin: str = "http://localhost:8000"
    tls_terminated_upstream: bool = False
    master_key_file: Path = Path(".data/master.key")
    turnstile_site_key: str = ""
    turnstile_secret: str = ""
    worker_api_host: str = "127.0.0.1"
    worker_api_port: int = DEFAULT_WORKER_API_PORT
    worker_api_workers: int = DEFAULT_WORKER_API_WORKERS
    worker_bootstrap_token: str = ""
    object_root: Path = Path(".data/objects")
    resend_api_key: str = ""
    mail_from: str = ""

    @classmethod
    def from_environment(cls) -> PortalSettings:
        return cls(
            database_dsn=_required("PORTAL_DATABASE_DSN"),
            environment=_required_choice("PORTAL_ENVIRONMENT", ENVIRONMENTS),
            public_origin=_required("PORTAL_PUBLIC_ORIGIN"),
            tls_terminated_upstream=_required_bool("PORTAL_TLS_TERMINATED_UPSTREAM"),
            master_key_file=Path(_required("PORTAL_MASTER_KEY_FILE")),
            turnstile_site_key=_optional("PORTAL_TURNSTILE_SITE_KEY"),
            turnstile_secret=_optional("PORTAL_TURNSTILE_SECRET"),
            worker_api_host=_optional("PORTAL_WORKER_API_HOST") or "127.0.0.1",
            worker_api_port=int(
                _optional("PORTAL_WORKER_API_PORT") or DEFAULT_WORKER_API_PORT
            ),
            worker_api_workers=int(
                _optional("PORTAL_WORKER_API_WORKERS") or DEFAULT_WORKER_API_WORKERS
            ),
            worker_bootstrap_token=_optional("PORTAL_WORKER_BOOTSTRAP_TOKEN"),
            object_root=Path(os.environ.get("PORTAL_OBJECT_ROOT", ".data/objects")),
            resend_api_key=_optional("PORTAL_RESEND_API_KEY"),
            mail_from=_optional("PORTAL_MAIL_FROM"),
        )

    def validate(self) -> None:
        if not self.database_dsn:
            msg = "PORTAL_DATABASE_DSN is required"
            raise RuntimeError(msg)

        if not urlparse(self.public_origin).hostname:
            msg = "PORTAL_PUBLIC_ORIGIN must include a hostname"
            raise RuntimeError(msg)

        if self.environment == "development":
            return

        self._validate_production()

    def _validate_production(self) -> None:
        problems = [
            requirement
            for requirement, satisfied in (
                ("PORTAL_PUBLIC_ORIGIN must use https", self.serves_https),
                ("PORTAL_TURNSTILE_SITE_KEY is required", self.turnstile_site_key),
                ("PORTAL_TURNSTILE_SECRET is required", self.turnstile_secret),
                (
                    "PORTAL_WORKER_BOOTSTRAP_TOKEN is required",
                    self.worker_bootstrap_token,
                ),
                ("PORTAL_RESEND_API_KEY is required", self.resend_api_key),
                ("PORTAL_MAIL_FROM is required", self.mail_from),
            )
            if not satisfied
        ]

        if problems:
            msg = f"production requires: {'; '.join(problems)}"
            raise RuntimeError(msg)

    @property
    def serves_https(self) -> bool:
        return urlparse(self.public_origin).scheme == "https"

    @property
    def session_cookie(self) -> str:
        return self._cookie("portal-id")

    @property
    def pending_mfa_cookie(self) -> str:
        return self._cookie("portal-mfa")

    @property
    def last_team_cookie(self) -> str:
        return self._cookie("portal-last-team")

    def _cookie(self, name: str) -> str:
        # The __Host- prefix binds a cookie to this exact origin and requires
        # Secure, so it is only available once the origin is https.
        return f"__Host-{name}" if self.serves_https else name

    @property
    def hostname(self) -> str:
        hostname = urlparse(self.public_origin).hostname
        assert hostname, "validated by PortalSettings.validate()"

        return hostname

    @property
    def allowed_hosts(self) -> tuple[str, ...]:
        """Host header values this origin answers to.

        Litestar matches the header verbatim, port included, so an origin that
        names a port has to allow both forms: the browser sends the port when
        it is not the scheme's default and omits it when it is.
        """
        netloc = urlparse(self.public_origin).netloc

        if netloc == self.hostname:
            return (netloc,)

        return (netloc, self.hostname)
