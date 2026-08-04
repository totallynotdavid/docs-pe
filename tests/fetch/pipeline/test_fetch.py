from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from fetch.domain.errors import (
    BanSignalError,
    RucNotFoundError,
    TransientTransportError,
)
from fetch.domain.policy import MAX_ATTEMPTS
from fetch.domain.types import Doc, DocKind, Site, SiteTuning, Status
from fetch.pipeline import session as session_mod
from fetch.pipeline.breaker import CircuitBreaker
from fetch.pipeline.fetch import fetch_one
from fetch.pipeline.session import WorkerConfig, WorkerState
from fetch.proxy.base import ProviderTuning, ProxySession


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import httpx

    from fetch.domain.types import Row


class _FakeProvider:
    name = "fake"
    tuning = ProviderTuning(workers=1, ban_cooldown_s=0.0)

    def __init__(self) -> None:
        self.released: list[str] = []
        self._n = 0

    def new_session(self, *, slot_id: int) -> ProxySession:
        self._n += 1
        return ProxySession(
            proxy_id=f"proxy-{self._n}",
            host="proxy.test",
            port="9999",
            username="u",
            password="p",
            session_id=f"sess-{self._n}",
        )

    async def release(self, session: ProxySession) -> None:
        self.released.append(session.proxy_id)


def _accepts_ruc(doc: Doc) -> bool:
    return doc.kind is DocKind.RUC


def _site(
    lookup: Callable[[httpx.AsyncClient, Doc], Awaitable[tuple[Row, ...]]],
    *,
    allows_empty: bool = True,
) -> Site:
    async def ready(client: httpx.AsyncClient, site: Site) -> None:
        return None

    return Site(
        name="fake_site",
        columns=("value",),
        accepts=_accepts_ruc,
        allows_empty=allows_empty,
        tuning=SiteTuning(session_budget=50),
        endpoints=(),
        ready=ready,
        lookup=lookup,
    )


def _cfg(*, ban_cooldown_s: float = 0.0) -> WorkerConfig:
    return WorkerConfig(
        session_budget=50, wait_min_s=0.0, wait_max_s=0.0, ban_cooldown_s=ban_cooldown_s
    )


@pytest.fixture(autouse=True)
def _no_real_egress_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    # _open_session probes the real egress IP on every session open; a unit test
    # must never make that live call.
    async def fake_resolve(proxy: ProxySession) -> str:
        return "1.2.3.4"

    monkeypatch.setattr(session_mod, "resolve_egress_ip", fake_resolve)


async def test_succeeds_on_the_first_attempt() -> None:
    async def lookup(client: httpx.AsyncClient, doc: Doc) -> tuple[Row, ...]:
        return (("value1",),)

    result = await fetch_one(
        site=_site(lookup),
        state=WorkerState(),
        doc=Doc("20100000001"),
        provider=_FakeProvider(),
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
    async def lookup(client: httpx.AsyncClient, doc: Doc) -> tuple[Row, ...]:
        msg = "no record"
        raise RucNotFoundError(msg)

    breaker = CircuitBreaker(provider="fake:fake", run_id="r")
    result = await fetch_one(
        site=_site(lookup),
        state=WorkerState(),
        doc=Doc("20100000001"),
        provider=_FakeProvider(),
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
    async def lookup(client: httpx.AsyncClient, doc: Doc) -> tuple[Row, ...]:
        return ()

    result = await fetch_one(
        site=_site(lookup, allows_empty=False),
        state=WorkerState(),
        doc=Doc("20100000001"),
        provider=_FakeProvider(),
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
    async def lookup(client: httpx.AsyncClient, doc: Doc) -> tuple[Row, ...]:
        raise exc

    result = await fetch_one(
        site=_site(lookup),
        state=WorkerState(),
        doc=Doc("20100000001"),
        provider=_FakeProvider(),
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
    async def lookup(client: httpx.AsyncClient, doc: Doc) -> tuple[Row, ...]:
        msg = "blocked"
        raise BanSignalError(msg)

    class _Clock:
        # Fake the clock the same way the session tests do, so the assertion is on
        # the exact cooldown boundary, not on a wall-clock race.
        def __init__(self, value: float) -> None:
            self.value = value

        def monotonic(self) -> float:
            return self.value

    clock = _Clock(1000.0)
    monkeypatch.setattr(session_mod, "time", clock)

    async def fake_sleep(seconds: float) -> None:
        clock.value += seconds

    monkeypatch.setattr(session_mod.asyncio, "sleep", fake_sleep)

    provider = _FakeProvider()
    state = WorkerState()
    result = await fetch_one(
        site=_site(lookup),
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
    async def lookup(client: httpx.AsyncClient, doc: Doc) -> tuple[Row, ...]:
        msg = "boom"
        raise TransientTransportError(msg)

    # threshold=1 trips on the very first failure, so every attempt after it sits
    # behind an open breaker.
    breaker = CircuitBreaker(
        provider="fake:fake", run_id="r", threshold=1, base_cooldown_s=0.01
    )
    result = await fetch_one(
        site=_site(lookup),
        state=WorkerState(),
        doc=Doc("20100000001"),
        provider=_FakeProvider(),
        breaker=breaker,
        slot_id=1,
        run_id="r",
        lane_id=1,
        cfg=_cfg(),
    )
    assert result.status is Status.FAILED
    assert result.made_healthy_contact is False
