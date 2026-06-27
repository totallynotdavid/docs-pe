from __future__ import annotations

import logging
import uuid

from dataclasses import dataclass
from os import getenv

from dotenv import load_dotenv

from robot.obs.events import SESSION_RELEASE_SKIPPED
from robot.obs.logging import kv
from robot.providers.proxy import ProviderTuning, ProxySession


logger = logging.getLogger(__name__)

_GATEWAY_HOST = "gw.dataimpulse.com"
# HTTP rotating port. Stickiness comes from the sessid in the username (see
# new_session), which keeps the port count irrelevant and lets lanes scale past
# the dedicated sticky-port range.
_HTTP_PORT = "823"
_DEFAULT_SESSTTL_MIN = 3

# Starting point only. These were NOT measured against DataImpulse yet; treat
# them as a conservative seed and confirm with a probe run before trusting them.
_TUNING = ProviderTuning(workers=10, ban_cooldown_s=30.0)


@dataclass(frozen=True)
class DataImpulseConfig:
    user: str
    password: str
    country: str
    sessttl: int
    host: str


def load_dataimpulse_config(*, env_file: str) -> DataImpulseConfig:
    load_dotenv(env_file, override=False)

    user = getenv("DATAIMPULSE_USER", "")
    password = getenv("DATAIMPULSE_PASS", "")
    # OSIPTEL's WAF blocks foreign exits, so default to Peru. DataImpulse country
    # codes are lowercase ISO-3166.
    country = getenv("DATAIMPULSE_COUNTRY", "pe").strip().lower()
    sessttl_raw = getenv("DATAIMPULSE_SESSTTL", "").strip()
    sessttl = int(sessttl_raw) if sessttl_raw else _DEFAULT_SESSTTL_MIN

    if not user or not password:
        msg = "missing DATAIMPULSE_USER or DATAIMPULSE_PASS"
        raise RuntimeError(msg)
    if not country:
        msg = "DATAIMPULSE_COUNTRY must not be empty"
        raise RuntimeError(msg)
    if sessttl < 1:
        msg = "DATAIMPULSE_SESSTTL must be >= 1 minute"
        raise RuntimeError(msg)

    return DataImpulseConfig(
        user=user,
        password=password,
        country=country,
        sessttl=sessttl,
        host=_GATEWAY_HOST,
    )


class DataImpulseProvider:
    """DataImpulse sticky sessions keyed by a per-session sessid in the username.

    A fresh sessid per call pins a new exit IP and rotates cleanly after a ban.
    Sessions expire by sessttl, so there is no release call to make.
    """

    name = "dataimpulse"
    tuning = _TUNING

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
        # No release API: DataImpulse sticky sessions expire by sessttl, so
        # dropping the reference is the whole teardown. Logged at debug so the
        # no-op is visible when chasing a leak without spamming normal runs.
        logger.debug(
            "%s %s",
            SESSION_RELEASE_SKIPPED,
            kv(
                provider=self.name,
                proxy_id=session.proxy_id,
                session_id=session.session_id,
            ),
        )
