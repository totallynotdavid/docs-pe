from __future__ import annotations

from dataclasses import dataclass

from robot.domain.errors import (
    BanSignalError,
    CaptchaError,
    CaptchaTimeoutError,
    ParseError,
    PermanentInputError,
    RobotError,
    TransientTransportError,
)


@dataclass(frozen=True)
class RetryDecision:
    error_code: str
    rotate_session: bool
    cooldown_proxy_s: float


def decide_retry(exc: RobotError, *, default_cooldown_s: float) -> RetryDecision:
    if isinstance(exc, PermanentInputError):
        return RetryDecision(
            error_code="permanent_input_error",
            rotate_session=False,
            cooldown_proxy_s=0.0,
        )
    if isinstance(exc, CaptchaTimeoutError):
        return RetryDecision(
            error_code="captcha_timeout",
            rotate_session=True,
            cooldown_proxy_s=min(default_cooldown_s, 30.0),
        )
    if isinstance(exc, CaptchaError):
        return RetryDecision(
            error_code="captcha_error",
            rotate_session=True,
            cooldown_proxy_s=default_cooldown_s,
        )
    if isinstance(exc, BanSignalError):
        return RetryDecision(
            error_code="ban_signal",
            rotate_session=True,
            cooldown_proxy_s=default_cooldown_s,
        )
    if isinstance(exc, ParseError):
        return RetryDecision(
            error_code="parse_error",
            rotate_session=True,
            cooldown_proxy_s=0.0,
        )
    if isinstance(exc, TransientTransportError):
        return RetryDecision(
            error_code="transport_error",
            rotate_session=True,
            cooldown_proxy_s=0.0,
        )
    return RetryDecision(
        error_code="provider_error",
        rotate_session=False,
        cooldown_proxy_s=0.0,
    )
