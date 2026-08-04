from __future__ import annotations

import pytest

from fetch.domain.errors import (
    BanSignalError,
    FetchError,
    ParseError,
    ProviderSchemaError,
    TransientTransportError,
    UpstreamNotReadyError,
)
from fetch.domain.policy import RetryDecision, classify_exception


def test_ban_signal_is_the_only_fault_that_carries_a_cooldown() -> None:
    decision = classify_exception(BanSignalError("blocked"), ban_cooldown_s=30.0)
    assert decision == RetryDecision("ban_signal", cooldown_s=30.0)


@pytest.mark.parametrize(
    ("exc", "expected_code"),
    [
        (UpstreamNotReadyError("x"), "upstream_not_ready"),
        (ParseError("x"), "parse_error"),
        (ProviderSchemaError("x"), "provider_schema_error"),
        (TransientTransportError("x"), "transport_error"),
        (FetchError("x"), "provider_error"),
    ],
)
def test_robot_errors_map_to_their_code_and_rotate_without_cooldown(
    exc: FetchError, expected_code: str
) -> None:
    decision = classify_exception(exc, ban_cooldown_s=30.0)
    assert decision == RetryDecision(expected_code, cooldown_s=0.0)


def test_a_leaked_non_robot_error_is_unknown_but_still_retried() -> None:
    # A bug in a lane must not tear down the run.
    decision = classify_exception(ValueError("leaked"), ban_cooldown_s=30.0)
    assert decision == RetryDecision("unknown_error", cooldown_s=0.0)
