from __future__ import annotations

from fastapi.testclient import TestClient
from portal.web.app import PortalSettings, create_app


class NotReady:
    async def ready(self) -> bool:
        return False


def test_health_and_readiness_are_small_operational_boundaries() -> None:
    app = create_app(PortalSettings(""), NotReady())
    with TestClient(app) as client:
        assert client.get("/salud").json() == {"estado": "saludable"}
        response = client.get("/listo")

    assert response.status_code == 503
    assert response.json() == {"estado": "no_listo"}
