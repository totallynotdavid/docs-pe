from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ProxySession:
    proxy_id: str
    host: str
    port: str
    username: str
    password: str
    session_id: str

    def as_http_proxy_url(self) -> str:
        return f"http://{self.username}:{self.password}@{self.host}:{self.port}"


@dataclass(frozen=True)
class ProviderTuning:
    # Proxy-owned operational defaults: workers is the vendor's concurrency plateau,
    # ban_cooldown_s parks a banned exit before the next acquisition.
    workers: int
    ban_cooldown_s: float


class ProxyProvider(Protocol):
    name: str
    tuning: ProviderTuning

    def new_session(self, *, slot_id: int) -> ProxySession: ...

    async def release(self, session: ProxySession) -> None: ...
