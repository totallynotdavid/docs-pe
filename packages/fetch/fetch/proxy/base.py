from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from fetch.domain.errors import ProxyConfigurationError


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


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
    # Vendor defaults. A lane count in PROXY_PROVIDER overrides workers per deployment.
    workers: int
    ban_cooldown_s: float


class ProxyProvider(Protocol):
    name: str
    tuning: ProviderTuning

    def new_session(self, *, slot_id: int) -> ProxySession: ...

    async def release(self, session: ProxySession) -> None: ...


@dataclass(frozen=True)
class Field:
    """One configuration input a provider needs.

    The single source for its three consumers: the environment loader (which reads
    `<PROVIDER>_<FIELD>`), the portal's credential form, and `ProviderSpec.build`.
    Human labels are user-facing copy, keyed by `(provider, field)` in the portal.
    """

    name: str
    secret: bool = False
    required: bool = True
    default: str = ""
    # Allowed raw values. Empty means free text; `normalize` still validates.
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProviderSpec:
    """A proxy vendor as a value: its field schema plus two functions.

    `normalize` validates raw strings from any source and returns canonical ones
    keyed by field name; `build` turns those into a live provider. Nothing
    downstream branches on the vendor.
    """

    name: str
    fields: tuple[Field, ...]
    tuning: ProviderTuning
    normalize: Callable[[Mapping[str, str]], dict[str, str]]
    build: Callable[[Mapping[str, str]], ProxyProvider]


def required(raw: Mapping[str, str], name: str) -> str:
    value = raw.get(name, "").strip()
    if not value:
        msg = f"{name} is required"
        raise ProxyConfigurationError(msg)
    return value


def optional(raw: Mapping[str, str], name: str) -> str:
    return raw.get(name, "").strip()


def one_of(raw: Mapping[str, str], name: str, choices: tuple[str, ...]) -> str:
    value = required(raw, name)
    if value not in choices:
        msg = f"{name} must be one of {'|'.join(choices)}"
        raise ProxyConfigurationError(msg)
    return value


def whole_number(
    raw: Mapping[str, str], name: str, *, minimum: int, maximum: int
) -> int:
    text = required(raw, name)
    try:
        value = int(text)
    except ValueError:
        msg = f"{name} must be a whole number"
        raise ProxyConfigurationError(msg) from None
    if not minimum <= value <= maximum:
        msg = f"{name} must be between {minimum} and {maximum}"
        raise ProxyConfigurationError(msg)
    return value


def country_code(raw: Mapping[str, str], name: str, *, lowercase: bool = False) -> str:
    value = required(raw, name)
    code = value.lower() if lowercase else value.upper()
    if len(code) != 2 or not code.isalpha():
        # OSIPTEL's WAF blocks foreign exits, so a malformed country would route
        # through the wrong region and fail every lookup. Reject it up front.
        msg = f"{name} must be a two-letter country code"
        raise ProxyConfigurationError(msg)
    return code


def flag(raw: Mapping[str, str], name: str) -> str:
    return "true" if raw.get(name, "").strip().lower() in {"1", "true", "yes"} else ""
