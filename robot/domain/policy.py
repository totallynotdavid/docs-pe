from __future__ import annotations

from dataclasses import dataclass

from robot.domain.errors import (
    BanSignalError,
    ParseError,
    ProviderSchemaError,
    RobotError,
    TransientTransportError,
    UpstreamNotReadyError,
)


# Per-run attempt budget for one RUC. Exhausting it does not retire the RUC.
MAX_ATTEMPTS = 4

# The single owner of the retirement rule. A failing RUC leaves the work set only
# by succeeding or by reaching this many cumulative healthy-contact attempts across
# all runs. No RobotError variant is classified as a permanent per-RUC failure, so
# every failure is environmental and stays eligible until then.
# Attempts made while the provider is unhealthy do not count (see
# Result.made_healthy_contact), so no outage can grind a RUC to this cap.
MAX_TOTAL_ATTEMPTS = 12


@dataclass(frozen=True)
class RetryDecision:
    # Only a ban sets cooldown_s (let the banned exit cool before reuse); every
    # other fault rotates immediately. error_code labels the fault for the log.
    error_code: str
    cooldown_s: float


def _classify(exc: RobotError, *, ban_cooldown_s: float) -> RetryDecision:
    if isinstance(exc, BanSignalError):
        return RetryDecision("ban_signal", cooldown_s=ban_cooldown_s)
    if isinstance(exc, UpstreamNotReadyError):
        return RetryDecision("upstream_not_ready", cooldown_s=0.0)
    if isinstance(exc, ParseError):
        return RetryDecision("parse_error", cooldown_s=0.0)
    if isinstance(exc, ProviderSchemaError):
        # Treated as transient, not permanent: an unexpected body is far more often
        # a site-wide maintenance or rejection page served to every RUC than a
        # genuinely malformed one, and calling it permanent would let one systemic
        # event retire the whole backlog. Rotate and let the breaker trip if it
        # correlates; a truly bad RUC still retires via the cap.
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
    if isinstance(exc, RobotError):
        return _classify(exc, ban_cooldown_s=ban_cooldown_s)
    return RetryDecision("unknown_error", cooldown_s=0.0)
