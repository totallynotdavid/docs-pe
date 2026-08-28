from __future__ import annotations

import logging
import uuid

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.obs.events import SESSION_RELEASE_SKIPPED
from core.obs.logging import kv
from core.proxy.base import (
    Field,
    ProviderSpec,
    ProviderTuning,
    ProxySession,
    country_code,
    required,
    whole_number,
)


if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

_GATEWAY_HOST = "gw.dataimpulse.com"

# Sticky sessions are encoded in the username, so every lane shares the same
# rotating HTTP endpoint.
_HTTP_PORT = "823"


@dataclass(frozen=True)
class DataImpulseConfig:
    user: str
    password: str
    country: str
    sessttl: int
    host: str


_FIELDS = (
    Field("username", secret=True),
    Field("password", secret=True),
    # DataImpulse expects lowercase ISO-3166 country codes.
    Field("country", default="pe"),
    Field("session_minutes", default="3", advanced=True),
)


def _normalize(raw: Mapping[str, str]) -> dict[str, str]:
    return {
        "username": required(raw, "username"),
        "password": required(raw, "password"),
        "country": country_code(raw, "country", lowercase=True),
        "session_minutes": str(
            whole_number(raw, "session_minutes", minimum=1, maximum=1440)
        ),
    }


def _build(values: Mapping[str, str]) -> DataImpulseProvider:
    return DataImpulseProvider(
        DataImpulseConfig(
            user=values["username"],
            password=values["password"],
            country=values["country"],
            sessttl=int(values["session_minutes"]),
            host=_GATEWAY_HOST,
        )
    )


DATAIMPULSE = ProviderSpec(
    name="dataimpulse",
    fields=_FIELDS,
    tuning=ProviderTuning(workers=18, ban_cooldown_s=30.0),
    normalize=_normalize,
    build=_build,
)


class DataImpulseProvider:
    """DataImpulse sessions keyed by a username `sessid`."""

    name = "dataimpulse"
    tuning = ProviderTuning(workers=18, ban_cooldown_s=30.0)

    def __init__(self, config: DataImpulseConfig) -> None:
        self._config = config

    def new_session(self, *, slot_id: int) -> ProxySession:
        config = self._config
        session_id = f"d{slot_id}_{uuid.uuid4().hex[:8]}"
        username = (
            f"{config.user}__cr.{config.country}"
            f";sessid.{session_id};sessttl.{config.sessttl}"
        )

        return ProxySession(
            proxy_id=f"dataimpulse-slot-{slot_id}",
            host=config.host,
            port=_HTTP_PORT,
            password=config.password,
            username=username,
            session_id=session_id,
        )

    async def release(self, session: ProxySession) -> None:
        # No release API exists, but keep session lifetimes observable.
        logger.debug(
            "%s %s",
            SESSION_RELEASE_SKIPPED,
            kv(
                provider=self.name,
                proxy_id=session.proxy_id,
                session_id=session.session_id,
            ),
        )
