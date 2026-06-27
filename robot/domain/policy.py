from __future__ import annotations

from dataclasses import dataclass

from robot.domain.errors import (
    BanSignalError,
    ParseError,
    PermanentInputError,
    ProviderSchemaError,
    RobotError,
    SessionStateError,
    TransientTransportError,
    UpstreamNotReadyError,
)


# Total attempts a single RUC gets within one run before its failure is
# persisted as terminal. Immediate ban rotation happens inside this budget, so
# after MAX_ATTEMPTS the RUC is recorded failed and not retried on re-launch.
MAX_ATTEMPTS = 4


@dataclass(frozen=True)
class RetryDecision:
    error_code: str
    # Three independent axes. retry controls whether the lane tries the same RUC
    # again; rotate controls whether it discards the proxy session first;
    # cooldown_s pauses the lane before acquiring the next session. They are not
    # the same question: a degraded upstream wants retry without rotation.
    retry: bool
    rotate: bool
    cooldown_s: float


def classify(exc: RobotError, *, ban_cooldown_s: float) -> RetryDecision:
    if isinstance(exc, SessionStateError):
        return RetryDecision(
            "session_state_error", retry=False, rotate=False, cooldown_s=0.0
        )
    if isinstance(exc, PermanentInputError):
        return RetryDecision(
            "permanent_input_error", retry=False, rotate=False, cooldown_s=0.0
        )
    if isinstance(exc, ProviderSchemaError):
        return RetryDecision(
            "provider_schema_error", retry=False, rotate=False, cooldown_s=0.0
        )
    if isinstance(exc, BanSignalError):
        return RetryDecision(
            "ban_signal", retry=True, rotate=True, cooldown_s=ban_cooldown_s
        )
    if isinstance(exc, UpstreamNotReadyError):
        return RetryDecision(
            "upstream_not_ready", retry=True, rotate=True, cooldown_s=0.0
        )
    if isinstance(exc, ParseError):
        return RetryDecision("parse_error", retry=True, rotate=True, cooldown_s=0.0)
    if isinstance(exc, TransientTransportError):
        # Transport-layer failures (SSL record-layer faults, connect resets,
        # read timeouts) are dominated by flaky proxy exits, not OSIPTEL itself.
        # Retrying on the same sticky session just burns all attempts against the
        # same bad exit and terminalizes a valid RUC, so rotate to a fresh proxy.
        return RetryDecision("transport_error", retry=True, rotate=True, cooldown_s=0.0)
    return RetryDecision("provider_error", retry=False, rotate=False, cooldown_s=0.0)
