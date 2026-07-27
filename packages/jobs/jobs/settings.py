from __future__ import annotations

import base64
import hashlib

from dataclasses import dataclass
from os import environ
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_path: Path
    object_root: Path
    session_secret: str
    bootstrap_admin_email: str | None = None
    bootstrap_admin_password: str | None = None
    worker_bootstrap_token: str | None = None
    encryption_key: str | None = None
    active_job_limit: int = 5
    cookie_secure: bool = False
    external_email_enabled: bool = False
    external_whatsapp_enabled: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        session_secret = environ.get("JOBS_SESSION_SECRET", "")
        if not session_secret:
            msg = "JOBS_SESSION_SECRET must be configured"
            raise RuntimeError(msg)
        limit = int(environ.get("JOBS_ACTIVE_JOB_LIMIT", "5"))
        if limit < 1 or limit > 5:
            msg = "JOBS_ACTIVE_JOB_LIMIT must be between 1 and 5"
            raise RuntimeError(msg)
        email_enabled = _enabled("JOBS_EMAIL_ENABLED")
        whatsapp_enabled = _enabled("JOBS_KAPSO_WHATSAPP_ENABLED")
        if email_enabled and not _blank_to_none(environ.get("JOBS_EMAIL_DSN", "")):
            msg = "JOBS_EMAIL_DSN is required when JOBS_EMAIL_ENABLED=true"
            raise RuntimeError(msg)
        if whatsapp_enabled and not _blank_to_none(
            environ.get("JOBS_KAPSO_API_KEY", "")
        ):
            msg = "JOBS_KAPSO_API_KEY is required when JOBS_KAPSO_WHATSAPP_ENABLED=true"
            raise RuntimeError(msg)
        return cls(
            database_path=Path(environ.get("JOBS_DATABASE_PATH", "var/jobs.sqlite3")),
            object_root=Path(environ.get("JOBS_OBJECT_ROOT", "var/objects")),
            session_secret=session_secret,
            bootstrap_admin_email=_blank_to_none(
                environ.get("JOBS_BOOTSTRAP_ADMIN_EMAIL", "")
            ),
            bootstrap_admin_password=_blank_to_none(
                environ.get("JOBS_BOOTSTRAP_ADMIN_PASSWORD", "")
            ),
            worker_bootstrap_token=_blank_to_none(
                environ.get("JOBS_WORKER_BOOTSTRAP_TOKEN", "")
            ),
            encryption_key=_blank_to_none(
                environ.get("JOBS_SECRET_ENCRYPTION_KEY", "")
            ),
            active_job_limit=limit,
            cookie_secure=_enabled("JOBS_COOKIE_SECURE"),
            external_email_enabled=email_enabled,
            external_whatsapp_enabled=whatsapp_enabled,
        )

    @property
    def fernet_key(self) -> bytes:
        """Derive a development key only when a deployment key was not supplied.

        Production deployments should set JOBS_SECRET_ENCRYPTION_KEY independently
        of the session secret. The deterministic fallback keeps local data readable
        across restarts without ever placing a provider secret in a job record.
        """
        if self.encryption_key:
            return self.encryption_key.encode("ascii")
        digest = hashlib.sha256(self.session_secret.encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest)


def _blank_to_none(value: str) -> str | None:
    return value.strip() or None


def _enabled(name: str) -> bool:
    return environ.get(name, "").lower() in {"1", "true", "yes"}
