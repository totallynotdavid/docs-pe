from __future__ import annotations

from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

import pytest

from fetch.domain.types import Doc, DocKind, Site, SiteTuning
from fetch.pipeline import session as session_mod
from fetch.proxy.base import ProviderTuning, ProxySession
from fetch.store.outcomes import OutcomeStore


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Iterator
    from pathlib import Path

    import httpx

    from fetch.domain.types import Projection, Row, RucKind


P = ParamSpec("P")
T = TypeVar("T")


def as_async(fn: Callable[P, T]) -> Callable[P, Coroutine[Any, Any, T]]:
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:  # noqa: RUF029
        return fn(*args, **kwargs)

    return wrapper


@pytest.fixture
def store(tmp_path: Path) -> Iterator[OutcomeStore]:
    with OutcomeStore(tmp_path / "run.state.sqlite3") as opened:
        yield opened


@pytest.fixture(autouse=True)
def _stub_egress_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    # Session setup probes the real egress IP, which unit tests must not call.
    monkeypatch.setattr(
        session_mod,
        "resolve_egress_ip",
        as_async(lambda proxy: "1.2.3.4"),
    )


def accepts_ruc(doc: Doc) -> bool:
    return doc.kind is DocKind.RUC


def accepts_ruc_kinds(*kinds: RucKind) -> Callable[[Doc], bool]:
    def accepts(doc: Doc) -> bool:
        return doc.kind is DocKind.RUC and doc.ruc_kind in kinds

    return accepts


class FakeClock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def monotonic(self) -> float:
        return self.value


def fake_proxy_session(
    *,
    proxy_id: str = "p1",
    session_id: str = "s1",
) -> ProxySession:
    return ProxySession(
        proxy_id=proxy_id,
        host="proxy.test",
        port="9999",
        username="u",
        password="p",
        session_id=session_id,
    )


class FakeProvider:
    name = "fake"

    def __init__(
        self,
        *,
        workers: int = 1,
        ban_cooldown_s: float = 0.0,
    ) -> None:
        self.tuning = ProviderTuning(
            workers=workers,
            ban_cooldown_s=ban_cooldown_s,
        )
        self.released: list[str] = []
        self.sessions_opened = 0

    def new_session(self, *, slot_id: int) -> ProxySession:
        self.sessions_opened += 1

        return fake_proxy_session(
            proxy_id=f"proxy-{self.sessions_opened}",
            session_id=f"sess-{self.sessions_opened}",
        )

    @as_async
    def release(self, session: ProxySession) -> None:
        self.released.append(session.proxy_id)


def fake_site(
    name: str = "fake_site",
    *columns: str,
    accepts: Callable[[Doc], bool] = accepts_ruc,
    allows_empty: bool = True,
    session_budget: int = 50,
    lookup: Callable[
        [httpx.AsyncClient, Doc],
        Awaitable[tuple[Row, ...]],
    ]
    | None = None,
    projections: tuple[Projection, ...] = (),
) -> Site:
    return Site(
        name=name,
        columns=columns,
        accepts=accepts,
        allows_empty=allows_empty,
        tuning=SiteTuning(session_budget=session_budget),
        endpoints=(),
        ready=as_async(lambda client, site: None),
        lookup=lookup or as_async(lambda client, doc: ()),
        projections=projections,
    )
