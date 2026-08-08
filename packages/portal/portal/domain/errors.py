from __future__ import annotations

from enum import StrEnum


class Reason(StrEnum):
    NOT_A_MEMBER = "not_a_member"
    LEADER_REQUIRED = "leader_required"
    SITE_ADMIN_REQUIRED = "site_admin_required"
    STEP_UP_REQUIRED = "step_up_required"
    CSRF_INVALID = "csrf_invalid"

    TEAM_NOT_FOUND = "team_not_found"
    JOB_NOT_FOUND = "job_not_found"
    USER_NOT_FOUND = "user_not_found"

    SOURCE_REQUIRED = "source_required"
    SOURCE_DUPLICATED = "source_duplicated"
    SOURCE_NOT_ENABLED = "source_not_enabled"

    SECRET_UNREADABLE = "secret_unreadable"
    WORKER_NOT_AUTHORIZED = "worker_not_authorized"
    WORKER_ID_INVALID = "worker_id_invalid"

    CREDENTIAL_REQUIRED = "credential_required"
    CREDENTIAL_WRONG_TEAM = "credential_wrong_team"
    CREDENTIAL_NOT_PENDING = "credential_not_pending"
    CREDENTIAL_STATE_INVALID = "credential_state_invalid"
    PROXY_UNAVAILABLE = "proxy_unavailable"
    PROXY_INVALID = "proxy_invalid"
    PROXY_PREFLIGHT_FAILED = "proxy_preflight_failed"

    INITIAL_TEAM_EXISTS = "initial_team_exists"
    INITIAL_TEAM_MISMATCH = "initial_team_mismatch"
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
    def __init__(self, reason: Reason, **params: object) -> None:
        super().__init__(reason.value)
        self.reason = reason
        self.params = params


class PermissionDenied(PortalError):
    pass


class StepUpRequired(PermissionDenied):
    """The actor is authorized but their second-factor proof is stale.

    A subclass of PermissionDenied rather than a sibling: it is audited by the
    same after_exception hook, and callers that only handle PermissionDenied
    still fail closed instead of falling through.
    """


class NotFound(PortalError):
    pass


class SourceValidationError(PortalError):
    pass


class FencedWrite(PortalError):
    pass


class ProvisioningError(PortalError):
    pass


class CredentialConfigurationError(PortalError):
    pass


class InputValidationError(PortalError):
    pass
