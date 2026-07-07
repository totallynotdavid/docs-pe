from __future__ import annotations

from typing import TYPE_CHECKING

from robot.pipeline import breaker as breaker_mod
from robot.pipeline.breaker import CircuitBreaker


if TYPE_CHECKING:
    import pytest


class _Clock:
    # A stand-in for the time module the breaker reads, so tests advance the clock
    # explicitly instead of sleeping and stay deterministic.
    def __init__(self, value: float) -> None:
        self.value = value

    def monotonic(self) -> float:
        return self.value


def _make(
    clock: _Clock,
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
    clock = _Clock(1000.0)
    cb = _make(clock, monkeypatch, threshold=3, base_cooldown_s=5.0)
    cb.record_failure()
    cb.record_failure()
    assert not cb.is_open()


def test_trips_open_at_the_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock(1000.0)
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
    clock = _Clock(1000.0)
    cb = _make(
        clock, monkeypatch, threshold=1, base_cooldown_s=5.0, max_cooldown_s=300.0
    )
    cb.record_failure()  # first trip: cooldown 5
    clock.value = 1005.1
    assert not cb.is_open()
    cb.record_failure()  # second trip: cooldown doubles to 10
    assert cb.is_open()
    clock.value = 1005.1 + 9.9
    assert cb.is_open()
    clock.value = 1005.1 + 10.1
    assert not cb.is_open()


def test_cooldown_is_capped_at_the_maximum(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock(1000.0)
    cb = _make(clock, monkeypatch, threshold=1, base_cooldown_s=5.0, max_cooldown_s=7.0)
    cb.record_failure()  # cooldown 5
    clock.value = 1005.1
    cb.record_failure()  # would be 10, capped at 7
    assert cb.is_open()
    clock.value = 1005.1 + 6.9
    assert cb.is_open()
    clock.value = 1005.1 + 7.1
    assert not cb.is_open()


def test_a_success_closes_the_breaker_and_resets_the_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(1000.0)
    cb = _make(clock, monkeypatch, threshold=1, base_cooldown_s=5.0)
    cb.record_failure()  # first trip, level 1
    cb.record_success()  # healthy contact closes it and resets the level
    assert not cb.is_open()
    cb.record_failure()  # next trip is back to the base cooldown, not doubled
    assert cb.is_open()
    clock.value = 1005.1
    assert not cb.is_open()


async def test_acquire_returns_immediately_when_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(1000.0)
    cb = _make(clock, monkeypatch, threshold=3)
    await cb.acquire()


async def test_acquire_waits_out_the_cooldown_before_returning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # acquire()'s own loop (open, sleep, recheck) is what every lane sits in while a
    # provider is unhealthy; fast-forward the fake clock on each sleep so the test
    # proves the loop actually terminates instead of hanging.
    clock = _Clock(1000.0)

    async def fake_sleep(seconds: float) -> None:  # noqa: RUF029
        clock.value += seconds

    monkeypatch.setattr(breaker_mod.asyncio, "sleep", fake_sleep)
    cb = _make(clock, monkeypatch, threshold=1, base_cooldown_s=5.0)
    cb.record_failure()
    assert cb.is_open()
    await cb.acquire()
    assert not cb.is_open()
