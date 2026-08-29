from __future__ import annotations

from dataclasses import dataclass

from core.domain.errors import (
    BanSignalError,
    FetchError,
    ParseError,
    ProviderSchemaError,
    TransientTransportError,
    UpstreamNotReadyError,
)


# Attempts allowed for one document within a run.
MAX_ATTEMPTS = 4

# A document retires only after success or this many cumulative attempts.
# Attempts blocked by an open circuit breaker do not count.
MAX_TOTAL_ATTEMPTS = 12


@dataclass(frozen=True)
class RetryDecision:
    error_code: str
    cooldown_s: float


def _classify(exc: FetchError, *, ban_cooldown_s: float) -> RetryDecision:
    if isinstance(exc, BanSignalError):
        return RetryDecision("ban_signal", cooldown_s=ban_cooldown_s)

    if isinstance(exc, UpstreamNotReadyError):
        return RetryDecision("upstream_not_ready", cooldown_s=0.0)

    if isinstance(exc, ParseError):
        return RetryDecision("parse_error", cooldown_s=0.0)

    if isinstance(exc, ProviderSchemaError):
        return RetryDecision("provider_schema_error", cooldown_s=0.0)

    if isinstance(exc, TransientTransportError):
        return RetryDecision("transport_error", cooldown_s=0.0)

    return RetryDecision("provider_error", cooldown_s=0.0)


def classify_exception(
    exc: BaseException,
    *,
    ban_cooldown_s: float,
) -> RetryDecision:
    """Map any lane exception to a retry decision.

    Unknown exceptions are retried as environmental faults instead of escaping
    the lane and cancelling the run.
    """
    if isinstance(exc, FetchError):
        return _classify(exc, ban_cooldown_s=ban_cooldown_s)

    return RetryDecision("unknown_error", cooldown_s=0.0)
