from __future__ import annotations

import asyncio

from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar, Protocol

import httpx

from portal.domain.errors import CredentialConfigurationError
from portal.domain.models import ProxyProvider


_PREFLIGHT_URL = "https://api.ipify.org?format=json"
_PREFLIGHT_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class ProxyField:
    name: str
    label: str
    secret: bool = False
    choices: tuple[tuple[str, str], ...] = ()
    default: str = ""
    required: bool = True


class ProxyProviderAdapter(Protocol):
    provider: ProxyProvider
    fields: tuple[ProxyField, ...]

    def normalize(self, raw: Mapping[str, str]) -> dict[str, str]: ...

    async def preflight(self, values: Mapping[str, str]) -> None: ...


def _value(raw: Mapping[str, str], name: str, *, required: bool = True) -> str:
    value = raw.get(name, "").strip()
    if required and not value:
        msg = "completa los campos requeridos"
        raise CredentialConfigurationError(msg)
    return value


def _country(value: str, *, lowercase: bool = False) -> str:
    normalized = value.lower() if lowercase else value.upper()
    if len(normalized) != 2 or not normalized.isalpha():
        msg = "el país debe usar un código de dos letras"
        raise CredentialConfigurationError(msg)
    return normalized


async def _proxy_preflight(proxy_url: str) -> None:
    """Perform one bounded egress request and never surface transport detail."""
    timeout = httpx.Timeout(_PREFLIGHT_TIMEOUT_SECONDS)
    failed = False
    try:
        async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout) as client:
            response = await client.get(_PREFLIGHT_URL)
            failed = response.status_code != 200
    except (httpx.HTTPError, OSError, asyncio.TimeoutError) as error:
        # Deliberately do not include a URL, account name, password, or provider body.
        msg = "no se pudo validar la conexión con el proveedor"
        raise CredentialConfigurationError(msg) from error
    if failed:
        msg = "no se pudo validar la conexión con el proveedor"
        raise CredentialConfigurationError(msg)


class GeoNodeProviderAdapter(ProxyProviderAdapter):
    provider: ProxyProvider = ProxyProvider.GEONODE
    fields = (
        ProxyField("username", "Usuario de GeoNode", secret=True),
        ProxyField("password", "Contraseña de GeoNode", secret=True),
        ProxyField(
            "gateway",
            "Puerta de enlace",
            choices=(
                ("fr", "Francia"),
                ("fr_whitelist", "Francia (lista permitida)"),
                ("us", "Estados Unidos"),
                ("sg", "Singapur"),
            ),
            default="fr",
        ),
        ProxyField(
            "proxy_type",
            "Tipo de red",
            choices=(
                ("residential", "Residencial"),
                ("datacenter", "Centro de datos"),
                ("mix", "Mixta"),
            ),
            default="residential",
        ),
        ProxyField("country", "País de salida", default="PE"),
        ProxyField("state", "Región", required=False),
        ProxyField("city", "Ciudad", required=False),
        ProxyField("asn", "ASN", required=False),
        ProxyField("lifetime_minutes", "Duración de sesión (minutos)", default="10"),
    )
    _hosts: ClassVar[dict[str, str]] = {
        "fr": "proxy.geonode.io",
        "fr_whitelist": "prod-proxy.geonode.io",
        "us": "us.proxy.geonode.io",
        "sg": "sg.proxy.geonode.io",
    }

    def normalize(self, raw: Mapping[str, str]) -> dict[str, str]:
        gateway = _value(raw, "gateway")
        proxy_type = _value(raw, "proxy_type")
        if gateway not in self._hosts or proxy_type not in {
            "residential",
            "datacenter",
            "mix",
        }:
            msg = "la selección de GeoNode no es válida"
            raise CredentialConfigurationError(msg)
        try:
            lifetime = int(_value(raw, "lifetime_minutes"))
        except ValueError as error:
            msg = "la duración de sesión debe ser un número"
            raise CredentialConfigurationError(msg) from error
        if not 3 <= lifetime <= 1440:
            msg = "la duración de sesión debe estar entre 3 y 1440 minutos"
            raise CredentialConfigurationError(msg)
        return {
            "username": _value(raw, "username"),
            "password": _value(raw, "password"),
            "gateway": gateway,
            "host": self._hosts[gateway],
            "port": "10000",
            "proxy_type": proxy_type,
            "country": _country(_value(raw, "country")),
            "state": _value(raw, "state", required=False),
            "city": _value(raw, "city", required=False),
            "asn": _value(raw, "asn", required=False),
            "lifetime_minutes": str(lifetime),
        }

    async def preflight(self, values: Mapping[str, str]) -> None:
        user = (
            f"{values['username']}-session-portalpreflight-type-{values['proxy_type']}"
            f"-country-{values['country']}-lifetime-{values['lifetime_minutes']}"
        )
        proxy_url = (
            f"http://{user}:{values['password']}@{values['host']}:{values['port']}"
        )
        await _proxy_preflight(proxy_url)


class DataImpulseProviderAdapter(ProxyProviderAdapter):
    provider: ProxyProvider = ProxyProvider.DATAIMPULSE
    fields = (
        ProxyField("username", "Usuario de DataImpulse", secret=True),
        ProxyField("password", "Contraseña de DataImpulse", secret=True),
        ProxyField("country", "País de salida", default="pe"),
        ProxyField("session_minutes", "Duración de sesión (minutos)", default="3"),
    )

    def normalize(self, raw: Mapping[str, str]) -> dict[str, str]:
        try:
            minutes = int(_value(raw, "session_minutes"))
        except ValueError as error:
            msg = "la duración de sesión debe ser un número"
            raise CredentialConfigurationError(msg) from error
        if minutes < 1:
            msg = "la duración de sesión debe ser de al menos un minuto"
            raise CredentialConfigurationError(msg)
        return {
            "username": _value(raw, "username"),
            "password": _value(raw, "password"),
            "host": "gw.dataimpulse.com",
            "port": "823",
            "country": _country(_value(raw, "country"), lowercase=True),
            "session_minutes": str(minutes),
        }

    async def preflight(self, values: Mapping[str, str]) -> None:
        user = (
            f"{values['username']}__cr.{values['country']};sessid.portalpreflight"
            f";sessttl.{values['session_minutes']}"
        )
        proxy_url = (
            f"http://{user}:{values['password']}@{values['host']}:{values['port']}"
        )
        await _proxy_preflight(proxy_url)


_PROVIDERS: dict[ProxyProvider, ProxyProviderAdapter] = {
    ProxyProvider.GEONODE: GeoNodeProviderAdapter(),
    ProxyProvider.DATAIMPULSE: DataImpulseProviderAdapter(),
}


def provider_for(provider: ProxyProvider | str) -> ProxyProviderAdapter:
    try:
        selected = ProxyProvider(provider)
    except ValueError as error:
        msg = "el proveedor seleccionado no está disponible"
        raise CredentialConfigurationError(msg) from error
    return _PROVIDERS[selected]
