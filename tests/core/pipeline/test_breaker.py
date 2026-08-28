from __future__ import annotations

from typing import TYPE_CHECKING

from core.pipeline import breaker as breaker_mod
from core.pipeline.breaker import CircuitBreaker

from tests.conftest import FakeClock, as_async


if TYPE_CHECKING:
    import pytest


def _make(
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    *,
    threshold: int = 10,
    base_cooldown_s: float = 5.0,
    max_cooldown_s: float = 300.0,
) -> CircuitBreaker:
    monkeypatch.setattr(breaker_mod, "time", clock)
    return CircuitBreaker(
        provider="osiptel:geonode",
        run_id="run",
        threshold=threshold,
        base_cooldown_s=base_cooldown_s,
        max_cooldown_s=max_cooldown_s,
    )


def test_stays_closed_below_the_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    cb = _make(clock, monkeypatch, threshold=3, base_cooldown_s=5.0)
    cb.record_failure()
    cb.record_failure()
    assert not cb.is_open()


def test_trips_open_at_the_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    cb = _make(clock, monkeypatch, threshold=3, base_cooldown_s=5.0)
    for _ in range(3):
        cb.record_failure()
    assert cb.is_open()
    clock.value = 1004.9
    assert cb.is_open()
    clock.value = 1005.1
    assert not cb.is_open()


def test_backoff_doubles_on_each_successive_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    cb = _make(
        clock, monkeypatch, threshold=1, base_cooldown_s=5.0, max_cooldown_s=300.0
    )
    cb.record_failure()
    clock.value = 1005.1
    assert not cb.is_open()
    cb.record_failure()
    assert cb.is_open()
    clock.value = 1005.1 + 9.9
    assert cb.is_open()
    clock.value = 1005.1 + 10.1
    assert not cb.is_open()


def test_cooldown_is_capped_at_the_maximum(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    cb = _make(clock, monkeypatch, threshold=1, base_cooldown_s=5.0, max_cooldown_s=7.0)
    cb.record_failure()
    clock.value = 1005.1
    cb.record_failure()
    assert cb.is_open()
    clock.value = 1005.1 + 6.9
    assert cb.is_open()
    clock.value = 1005.1 + 7.1
    assert not cb.is_open()


def test_a_success_closes_the_breaker_and_resets_the_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    cb = _make(clock, monkeypatch, threshold=1, base_cooldown_s=5.0)
    cb.record_failure()
    cb.record_success()
    assert not cb.is_open()
    cb.record_failure()
    assert cb.is_open()
    clock.value = 1005.1
    assert not cb.is_open()


async def test_acquire_returns_immediately_when_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    cb = _make(clock, monkeypatch, threshold=3)
    await cb.acquire()


async def test_acquire_waits_out_the_cooldown_before_returning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # acquire()'s own loop (open, sleep, recheck) is what every lane sits in while a
    # provider is unhealthy; this pins that the loop actually terminates.
    clock = FakeClock()

    def advance(seconds: float) -> None:
        clock.value += seconds

    monkeypatch.setattr(breaker_mod.asyncio, "sleep", as_async(advance))
    cb = _make(clock, monkeypatch, threshold=1, base_cooldown_s=5.0)
    cb.record_failure()
    assert cb.is_open()
    await cb.acquire()
    assert not cb.is_open()
