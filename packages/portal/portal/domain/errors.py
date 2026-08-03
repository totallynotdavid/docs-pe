"""Errors mapped by the web boundary. An error names a `Reason`, never a sentence."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class Reason(StrEnum):
    NOT_A_MEMBER = "not_a_member"
    LEADER_REQUIRED = "leader_required"
    SITE_ADMIN_REQUIRED = "site_admin_required"
    CSRF_INVALID = "csrf_invalid"

    TEAM_NOT_FOUND = "team_not_found"
    JOB_NOT_FOUND = "job_not_found"
    USER_NOT_FOUND = "user_not_found"

    SOURCE_REQUIRED = "source_required"
    SOURCE_DUPLICATED = "source_duplicated"
    SOURCE_NOT_ENABLED = "source_not_enabled"

    CREDENTIAL_REQUIRED = "credential_required"
    CREDENTIAL_WRONG_TEAM = "credential_wrong_team"
    CREDENTIAL_NOT_PENDING = "credential_not_pending"
    CREDENTIAL_STATE_INVALID = "credential_state_invalid"
    PROXY_UNAVAILABLE = "proxy_unavailable"
    PROXY_INVALID = "proxy_invalid"
    PROXY_PREFLIGHT_FAILED = "proxy_preflight_failed"

    INITIAL_TEAM_EXISTS = "initial_team_exists"
    TEAM_MISSING = "team_missing"
    TEAM_NAME_LENGTH = "team_name_length"
    SLUG_INVALID = "slug_invalid"
    EMAIL_INVALID = "email_invalid"
    LABEL_LENGTH = "label_length"
    PASSWORD_TOO_SHORT = "password_too_short"
    ROLE_INVALID = "role_invalid"
    LAST_LEADER = "last_leader"

    WORKER_SOURCE_REQUIRED = "worker_source_required"

    CSV_REQUIRED = "csv_required"
    CSV_EXTENSION = "csv_extension"
    CSV_EMPTY = "csv_empty"
    CSV_TOO_LARGE = "csv_too_large"
    CSV_ENCODING = "csv_encoding"
    CSV_UNREADABLE = "csv_unreadable"


class PortalError(Exception):
    """Base application error, identified by reason rather than by message."""

    def __init__(self, reason: Reason, **params: Any) -> None:
        super().__init__(reason.value)
        self.reason = reason
        self.params = params


class PermissionDenied(PortalError):
    """The actor cannot perform an operation for this team."""


class NotFound(PortalError):
    """The requested team-scoped resource is unavailable to the actor."""


class SourceValidationError(PortalError):
    """A submission selected a source outside the stable fetch adapters."""


class FencedWrite(PortalError):
    """A worker attempted a result write after its job lease changed."""


class ProvisioningError(PortalError):
    """A durable installation or team setup command could not be completed."""


class CredentialConfigurationError(PortalError):
    """A proxy configuration is invalid or could not be validated safely."""


class InputValidationError(PortalError):
    """An uploaded file or form value could not be accepted."""
