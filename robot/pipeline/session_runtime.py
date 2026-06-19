from __future__ import annotations

import logging
import random
import time

from dataclasses import dataclass

from robot.obs.events import (
    EGRESS_IP_UNRESOLVED,
    SESSION_READY,
    STICKY_ACQUIRE,
    STICKY_RELEASE_FAILED,
)
from robot.obs.logging import kv
from robot.providers.geonode import (
    GeoNodeConfig,
    ProxySessionConfig,
    new_proxy_session,
    release_proxy_session,
    resolve_egress_ip,
)
from robot.providers.osiptel_session import OsiptelSession, OsiptelSessionSettings


logger = logging.getLogger(__name__)


@dataclass
class ActiveSession:
    session: ProxySessionConfig
    osiptel: OsiptelSession
    uses: int = 0
    egress_ip: str = ""
    egress_ip_warned: bool = False


class SessionRuntime:
    def __init__(
        self,
        *,
        run_id: str,
        worker_id: int,
        slot_id: int,
        geonode: GeoNodeConfig,
        session_budget: int,
        wait_min_s: float,
        wait_max_s: float,
    ) -> None:
        self._run_id = run_id
        self._worker_id = worker_id
        self._slot_id = slot_id
        self._geonode = geonode
        self._session_budget = session_budget
        self._wait_min_s = wait_min_s
        self._wait_max_s = wait_max_s
        self._active: ActiveSession | None = None
        self._last_proxy_id = ""
        self._cooldown_until = 0.0

    def ensure_active(self) -> ActiveSession:
        if self._active is not None:
            return self._active

        remaining = self._cooldown_until - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)

        session = new_proxy_session(self._geonode, slot_id=self._slot_id)
        self._last_proxy_id = session.proxy_id
        logger.info(
            "%s %s",
            STICKY_ACQUIRE,
            kv(
                proxy_id=session.proxy_id,
                session_id=session.session_id,
                port=session.port,
                slot_id=self._slot_id,
            ),
        )
        osiptel = OsiptelSession(
            proxy=session,
            settings=OsiptelSessionSettings(),
        )
        try:
            osiptel.open()
            egress_ip = resolve_egress_ip(session)
        except Exception:
            osiptel.close()
            self._release_session(session)
            raise

        self._active = ActiveSession(
            session=session,
            osiptel=osiptel,
            uses=0,
            egress_ip=egress_ip,
        )
        final_egress_ip = self.refresh_egress_ip()
        logger.info(
            "%s %s",
            SESSION_READY,
            kv(
                run_id=self._run_id,
                worker_id=self._worker_id,
                session_id=osiptel.session_id,
                proxy_id=osiptel.proxy_id,
                egress_ip=final_egress_ip,
            ),
        )
        return self._active

    def after_success(self) -> None:
        if self._active is None:
            return

        self._active.uses += 1
        if self._active.uses >= self._session_budget:
            self.close_active(cooldown_s=0.0)
            return

        wait_s = random.uniform(self._wait_min_s, self._wait_max_s)
        time.sleep(wait_s)

    def close_active(self, *, cooldown_s: float) -> None:
        if self._active is None:
            return

        active = self._active
        self._active = None
        active.osiptel.close()
        self._release_session(active.session)
        if cooldown_s > 0:
            self._cooldown_until = max(
                self._cooldown_until,
                time.monotonic() + cooldown_s,
            )

    def active_session_id(self) -> str:
        if self._active is None:
            return ""
        return self._active.osiptel.session_id

    def active_egress_ip(self) -> str:
        if self._active is None:
            return ""
        return self._active.egress_ip

    def refresh_egress_ip(self) -> str:
        if self._active is None:
            return ""
        if self._active.egress_ip:
            return self._active.egress_ip

        resolved = resolve_egress_ip(self._active.session)
        if resolved:
            self._active.egress_ip = resolved
            self._active.egress_ip_warned = False
            return resolved
        if not self._active.egress_ip_warned:
            logger.warning(
                "%s %s",
                EGRESS_IP_UNRESOLVED,
                kv(
                    run_id=self._run_id,
                    worker_id=self._worker_id,
                    session_id=self._active.osiptel.session_id,
                    proxy_id=self._active.osiptel.proxy_id,
                ),
            )
            self._active.egress_ip_warned = True
        return ""

    @property
    def last_proxy_id(self) -> str:
        return self._last_proxy_id

    def _release_session(self, session: ProxySessionConfig) -> None:
        last_status = 0
        last_error = ""
        for attempt in range(1, 4):
            ok, status, error = release_proxy_session(
                config=self._geonode,
                session_id=session.session_id,
                port=int(session.port),
                timeout_s=10.0,
            )
            if ok:
                return
            last_status = status
            last_error = error
            if attempt < 3:
                time.sleep(0.5 * attempt)

        logger.warning(
            "%s %s",
            STICKY_RELEASE_FAILED,
            kv(
                proxy_id=session.proxy_id,
                session_id=session.session_id,
                port=session.port,
                status=last_status,
                error=last_error,
                attempts=3,
            ),
        )
