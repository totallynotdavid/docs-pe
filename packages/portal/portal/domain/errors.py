"""Errors deliberately mapped by the web boundary, never by the domain."""


class PortalError(Exception):
    """Base application error."""


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
