from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from fetch.domain.errors import (
    BanSignalError,
    RucNotFoundError,
    TransientTransportError,
)
from fetch.domain.policy import MAX_ATTEMPTS
from fetch.domain.types import Doc, Status
from fetch.pipeline import session as session_mod
from fetch.pipeline.breaker import CircuitBreaker
from fetch.pipeline.fetch import fetch_one
from fetch.pipeline.session import WorkerConfig, WorkerState

from tests.fetch.conftest import FakeClock, FakeProvider, as_async, fake_site


if TYPE_CHECKING:
    import httpx

    from fetch.domain.types import Row


def _cfg(*, ban_cooldown_s: float = 0.0) -> WorkerConfig:
    return WorkerConfig(
        session_budget=50, wait_min_s=0.0, wait_max_s=0.0, ban_cooldown_s=ban_cooldown_s
    )


async def test_succeeds_on_the_first_attempt() -> None:
    def lookup(client: httpx.AsyncClient, doc: Doc) -> tuple[Row, ...]:
        return (("value1",),)

    result = await fetch_one(
        site=fake_site("fake_site", "value", lookup=as_async(lookup)),
        state=WorkerState(),
        doc=Doc("20100000001"),
        provider=FakeProvider(),
        breaker=CircuitBreaker(provider="fake:fake", run_id="r"),
        slot_id=1,
        run_id="r",
        lane_id=1,
        cfg=_cfg(),
    )
    assert result.status is Status.OK
    assert result.rows == (("value1",),)
    assert result.attempt == 1


async def test_a_ruc_not_found_error_is_a_terminal_not_found_with_no_retry() -> None:
    def lookup(client: httpx.AsyncClient, doc: Doc) -> tuple[Row, ...]:
        msg = "no record"
        raise RucNotFoundError(msg)

    breaker = CircuitBreaker(provider="fake:fake", run_id="r")
    result = await fetch_one(
        site=fake_site("fake_site", "value", lookup=as_async(lookup)),
        state=WorkerState(),
        doc=Doc("20100000001"),
        provider=FakeProvider(),
        breaker=breaker,
        slot_id=1,
        run_id="r",
        lane_id=1,
        cfg=_cfg(),
    )
    assert result.status is Status.NOT_FOUND
    assert result.attempt == 1
    assert not breaker.is_open()


async def test_allows_empty_false_and_empty_rows_retries_then_fails() -> None:
    def lookup(client: httpx.AsyncClient, doc: Doc) -> tuple[Row, ...]:
        return ()

    result = await fetch_one(
        site=fake_site(
            "fake_site", "value", lookup=as_async(lookup), allows_empty=False
        ),
        state=WorkerState(),
        doc=Doc("20100000001"),
        provider=FakeProvider(),
        breaker=CircuitBreaker(provider="fake:fake", run_id="r"),
        slot_id=1,
        run_id="r",
        lane_id=1,
        cfg=_cfg(),
    )
    assert result.status is Status.FAILED
    assert result.error_code == "provider_schema_error"
    assert result.attempt == MAX_ATTEMPTS


@pytest.mark.parametrize(
    ("exc", "expected_code"),
    [
        (TransientTransportError("boom"), "transport_error"),
        (ValueError("weird"), "unknown_error"),
    ],
)
async def test_a_fault_retries_up_to_max_attempts_with_the_classified_code(
    exc: BaseException, expected_code: str
) -> None:
    def lookup(client: httpx.AsyncClient, doc: Doc) -> tuple[Row, ...]:
        raise exc

    result = await fetch_one(
        site=fake_site("fake_site", "value", lookup=as_async(lookup)),
        state=WorkerState(),
        doc=Doc("20100000001"),
        provider=FakeProvider(),
        breaker=CircuitBreaker(provider="fake:fake", run_id="r"),
        slot_id=1,
        run_id="r",
        lane_id=1,
        cfg=_cfg(),
    )
    assert result.status is Status.FAILED
    assert result.error_code == expected_code
    assert result.attempt == MAX_ATTEMPTS


async def test_a_ban_error_rotates_the_session_with_a_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def lookup(client: httpx.AsyncClient, doc: Doc) -> tuple[Row, ...]:
        msg = "blocked"
        raise BanSignalError(msg)

    clock = FakeClock()
    monkeypatch.setattr(session_mod, "time", clock)

    def advance(seconds: float) -> None:
        clock.value += seconds

    monkeypatch.setattr(session_mod.asyncio, "sleep", as_async(advance))

    provider = FakeProvider()
    state = WorkerState()
    result = await fetch_one(
        site=fake_site("fake_site", "value", lookup=as_async(lookup)),
        state=state,
        doc=Doc("20100000001"),
        provider=provider,
        breaker=CircuitBreaker(provider="fake:fake", run_id="r"),
        slot_id=1,
        run_id="r",
        lane_id=1,
        cfg=_cfg(ban_cooldown_s=2.5),
    )
    assert result.status is Status.FAILED
    assert result.error_code == "ban_signal"
    # A ban rotates the session on every attempt.
    assert len(provider.released) == MAX_ATTEMPTS
    # The cooldown deadline never shrinks; each failure adds one ban cooldown.
    assert state.cooldown_until == pytest.approx(1000.0 + 2.5 * MAX_ATTEMPTS)


async def test_made_healthy_contact_is_false_once_the_breaker_trips() -> None:
    def lookup(client: httpx.AsyncClient, doc: Doc) -> tuple[Row, ...]:
        msg = "boom"
        raise TransientTransportError(msg)

    # threshold=1 trips on the very first failure, so every attempt after it sits
    # behind an open breaker.
    breaker = CircuitBreaker(
        provider="fake:fake", run_id="r", threshold=1, base_cooldown_s=0.01
    )
    result = await fetch_one(
        site=fake_site("fake_site", "value", lookup=as_async(lookup)),
        state=WorkerState(),
        doc=Doc("20100000001"),
        provider=FakeProvider(),
        breaker=breaker,
        slot_id=1,
        run_id="r",
        lane_id=1,
        cfg=_cfg(),
    )
    assert result.status is Status.FAILED
    assert result.made_healthy_contact is False
