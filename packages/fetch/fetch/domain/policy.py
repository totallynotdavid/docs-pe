from __future__ import annotations

from dataclasses import dataclass

from fetch.domain.errors import (
    BanSignalError,
    FetchError,
    ParseError,
    ProviderSchemaError,
    TransientTransportError,
    UpstreamNotReadyError,
)


# Per-run attempt budget for one document. Exhausting it does not retire the document.
MAX_ATTEMPTS = 4

# The single owner of the retirement rule: a document leaves the work set by succeeding
# or by reaching this many cumulative healthy-contact attempts across all runs. Every
# FetchError is treated as environmental, so nothing else retires a document. Attempts
# that hit a tripped breaker are excluded, so an outage cannot grind one out.
MAX_TOTAL_ATTEMPTS = 12


@dataclass(frozen=True)
class RetryDecision:
    # Only a ban sets cooldown_s (let the banned exit cool before reuse); every
    # other fault rotates immediately.
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
        # An unexpected body is usually a site-wide maintenance or rejection page, so
        # rotate. A uniformly broken site trips the breaker; one bad document retires
        # via MAX_TOTAL_ATTEMPTS.
        return RetryDecision("provider_schema_error", cooldown_s=0.0)
    if isinstance(exc, TransientTransportError):
        return RetryDecision("transport_error", cooldown_s=0.0)
    return RetryDecision("provider_error", cooldown_s=0.0)


def classify_exception(exc: BaseException, *, ban_cooldown_s: float) -> RetryDecision:
    """Classify any lane exception, mapped or not.

    Anything outside the FetchError taxonomy is treated as an unknown environmental
    fault and retried, so no stray exception escapes a lane and tears down the run.
    """
    if isinstance(exc, FetchError):
        return _classify(exc, ban_cooldown_s=ban_cooldown_s)
    return RetryDecision("unknown_error", cooldown_s=0.0)
