from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

from fetch.domain.types import DocKind, Site, SiteTuning
from fetch.pipeline import session as session_mod
from fetch.pipeline.session import (
    WorkerConfig,
    WorkerSession,
    WorkerState,
    after_success,
    close_session,
    ensure_session,
    rotate_session,
    session_ids,
)
from fetch.proxy.base import ProviderTuning, ProxySession


if TYPE_CHECKING:
    from fetch.domain.types import Doc, Row


def _accepts_ruc(doc: Doc) -> bool:
    return doc.kind is DocKind.RUC


class _Clock:
    def __init__(self, value: float) -> None:
        self.value = value

    def monotonic(self) -> float:
        return self.value


class _FakeProvider:
    name = "fake"
    tuning = ProviderTuning(workers=1, ban_cooldown_s=0.0)

    def __init__(self) -> None:
        self.released: list[str] = []
        self.new_session_calls = 0

    def new_session(self, *, slot_id: int) -> ProxySession:
        self.new_session_calls += 1

        return ProxySession(
            proxy_id=f"proxy-{self.new_session_calls}",
            host="proxy.test",
            port="9999",
            username="u",
            password="p",
            session_id=f"sess-{self.new_session_calls}",
        )

    async def release(self, session: ProxySession) -> None:
        self.released.append(session.proxy_id)


def _site() -> Site:
    async def ready(
        _client: httpx.AsyncClient,
        _site: Site,
    ) -> None:
        return None

    async def lookup(
        _client: httpx.AsyncClient,
        _doc: Doc,
    ) -> tuple[Row, ...]:
        return ()

    return Site(
        name="fake_site",
        columns=(),
        accepts=_accepts_ruc,
        allows_empty=True,
        tuning=SiteTuning(session_budget=50),
        endpoints=(),
        ready=ready,
        lookup=lookup,
    )


def _worker_session(*, proxy_id: str = "p1") -> WorkerSession:
    return WorkerSession(
        proxy=ProxySession(
            proxy_id=proxy_id,
            host="proxy.test",
            port="9999",
            username="u",
            password="p",
            session_id="sess-1",
        ),
        client=httpx.AsyncClient(),
        session_id="existing-session",
        egress_ip="9.9.9.9",
    )


@pytest.fixture(autouse=True)
def _no_real_egress_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_resolve(_proxy: ProxySession) -> str:
        return "1.2.3.4"

    monkeypatch.setattr(session_mod, "resolve_egress_ip", fake_resolve)


async def test_ensure_session_reuses_an_open_session() -> None:
    existing = _worker_session()
    state = WorkerState(session=existing)
    provider = _FakeProvider()

    session = await ensure_session(
        state,
        site=_site(),
        provider=provider,
        slot_id=1,
        run_id="r",
        lane_id=1,
    )

    assert session is existing
    assert provider.new_session_calls == 0

    await existing.client.aclose()


async def test_ensure_session_waits_out_a_pending_cooldown_before_opening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(1000.0)
    waited: list[float] = []

    monkeypatch.setattr(session_mod, "time", clock)

    async def fake_sleep(seconds: float) -> None:
        waited.append(seconds)
        clock.value += seconds

    monkeypatch.setattr(session_mod.asyncio, "sleep", fake_sleep)

    state = WorkerState(cooldown_until=1005.0)
    provider = _FakeProvider()

    session = await ensure_session(
        state,
        site=_site(),
        provider=provider,
        slot_id=1,
        run_id="r",
        lane_id=1,
    )

    assert waited == [5.0]
    assert provider.new_session_calls == 1
    assert state.uses == 0

    await session.client.aclose()


async def test_after_success_increments_uses_without_closing_below_budget() -> None:
    session = _worker_session()
    state = WorkerState(session=session, uses=0)
    provider = _FakeProvider()
    cfg = WorkerConfig(
        session_budget=3,
        wait_min_s=0.0,
        wait_max_s=0.0,
        ban_cooldown_s=0.0,
    )

    await after_success(state, provider=provider, cfg=cfg)

    assert state.uses == 1
    assert state.session is session
    assert provider.released == []

    await session.client.aclose()


async def test_after_success_closes_once_the_budget_is_reached() -> None:
    session = _worker_session(proxy_id="p1")
    state = WorkerState(session=session, uses=2)
    provider = _FakeProvider()
    cfg = WorkerConfig(
        session_budget=3,
        wait_min_s=0.0,
        wait_max_s=0.0,
        ban_cooldown_s=0.0,
    )

    await after_success(state, provider=provider, cfg=cfg)

    assert state.uses == 3
    assert state.session is None
    assert provider.released == ["p1"]


async def test_rotate_session_closes_and_sets_a_fresh_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(1000.0)
    monkeypatch.setattr(session_mod, "time", clock)

    session = _worker_session(proxy_id="p1")
    state = WorkerState(session=session)
    provider = _FakeProvider()

    await rotate_session(state, provider=provider, cooldown_s=10.0)

    assert state.session is None
    assert state.last_proxy_id == "p1"
    assert provider.released == ["p1"]
    assert state.cooldown_until == pytest.approx(1010.0)


async def test_rotate_session_never_shrinks_a_longer_pending_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(1000.0)
    monkeypatch.setattr(session_mod, "time", clock)

    state = WorkerState(session=_worker_session(), cooldown_until=1050.0)
    provider = _FakeProvider()

    await rotate_session(state, provider=provider, cooldown_s=5.0)

    assert state.cooldown_until == pytest.approx(1050.0)


async def test_close_session_calls_release_and_clears_state() -> None:
    session = _worker_session(proxy_id="p9")
    state = WorkerState(session=session)
    provider = _FakeProvider()

    await close_session(state, provider=provider)

    assert state.session is None
    assert provider.released == ["p9"]


async def test_close_session_is_a_no_op_once_already_closed() -> None:
    state = WorkerState(session=None)
    provider = _FakeProvider()

    await close_session(state, provider=provider)

    assert provider.released == []


def test_session_ids_reflects_the_open_session() -> None:
    session = _worker_session(proxy_id="p1")
    state = WorkerState(session=session)

    assert session_ids(state) == ("existing-session", "p1")


def test_session_ids_falls_back_to_the_last_known_proxy_when_closed() -> None:
    state = WorkerState(session=None, last_proxy_id="p1")

    assert session_ids(state) == ("", "p1")
