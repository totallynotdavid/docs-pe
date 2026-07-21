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

# The single owner of the retirement rule. A failing document leaves the work set only
# by succeeding or by reaching 12 cumulative healthy-contact attempts across all
# runs. No FetchError variant is classified as a permanent per-document failure, so
# every failure is environmental and stays eligible until then.
# Attempts that hit a tripped breaker do not count toward the cap, so no outage can
# grind a document out by exhausting the budget.
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
        # An unexpected body is far more often a site-wide maintenance or rejection
        # page than a genuinely malformed one, so rotate instead of treating it as a
        # per-document failure. The breaker will trip if the site is uniformly broken;
        # a single bad document still retires via MAX_TOTAL_ATTEMPTS.
        return RetryDecision("provider_schema_error", cooldown_s=0.0)
    if isinstance(exc, TransientTransportError):
        return RetryDecision("transport_error", cooldown_s=0.0)
    return RetryDecision("provider_error", cooldown_s=0.0)


def classify_exception(exc: BaseException, *, ban_cooldown_s: float) -> RetryDecision:
    """Classify any lane exception, mapped or not.

    RobotErrors carry their own taxonomy. Anything else (a raw transport fault
    that slipped past normalization, a bug in a code path a lane touches) is an
    unknown environmental fault: rotate and retry rather than let it escape the
    lane and tear down the run.
    """
    if isinstance(exc, FetchError):
        return _classify(exc, ban_cooldown_s=ban_cooldown_s)
    return RetryDecision("unknown_error", cooldown_s=0.0)
