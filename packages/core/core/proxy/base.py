from __future__ import annotations

from dataclasses import dataclass
from os import getenv
from typing import TYPE_CHECKING, Protocol

from core.domain.errors import ProxyConfigurationError


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
    workers: int
    ban_cooldown_s: float
    # Total slot_id values the provider can hand distinct real ports for. None
    # means slot_id has no fleet-wide meaning (safe to assign locally per lane).
    slot_pool: int | None = None


class ProxyProvider(Protocol):
    name: str
    tuning: ProviderTuning

    def new_session(self, *, slot_id: int) -> ProxySession: ...

    async def release(self, session: ProxySession) -> None: ...


@dataclass(frozen=True)
class Field:
    """One configuration field shared by loading, validation, and the portal."""

    name: str
    secret: bool = False
    required: bool = True
    default: str = ""
    choices: tuple[str, ...] = ()
    advanced: bool = False


@dataclass(frozen=True)
class ProviderSpec:
    """A proxy provider's schema, tuning, validator, and constructor."""

    name: str
    fields: tuple[Field, ...]
    tuning: ProviderTuning
    normalize: Callable[[Mapping[str, str]], dict[str, str]]
    build: Callable[[Mapping[str, str]], ProxyProvider]


def values_from_environment(spec: ProviderSpec) -> dict[str, str]:
    """A provider's field values, read from {SPEC_NAME}_{FIELD_NAME} and normalized."""
    raw = {
        field.name: getenv(f"{spec.name}_{field.name}".upper(), field.default)
        for field in spec.fields
    }

    return spec.normalize(raw)


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
        msg = f"{name} must be a two-letter country code"
        raise ProxyConfigurationError(msg)
    return code


def flag(raw: Mapping[str, str], name: str) -> str:
    value = raw.get(name, "").strip().lower()
    return "true" if value in {"1", "true", "yes"} else ""
