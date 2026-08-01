from __future__ import annotations

import io

import pytest

from fastapi import UploadFile
from fastapi.testclient import TestClient
from portal.web.app import (
    MAX_CSV_UPLOAD_BYTES,
    PortalSettings,
    create_app,
    read_csv_upload,
)


class NotReady:
    async def ready(self) -> bool:
        return False


def _upload(name: str, content: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(content), filename=name)


def test_health_and_readiness_are_small_operational_boundaries() -> None:
    app = create_app(PortalSettings(""), NotReady())
    with TestClient(app) as client:
        assert client.get("/salud").json() == {"estado": "saludable"}
        response = client.get("/listo")

    assert response.status_code == 503
    assert response.json() == {"estado": "no_listo"}


async def test_csv_upload_accepts_a_valid_file_and_strips_its_directories() -> None:
    name, content = await read_csv_upload(_upload("../../barranca.CSV", b"10412345678"))

    assert name == "barranca.CSV"
    assert content == b"10412345678"


@pytest.mark.parametrize(
    ("upload", "message"),
    [
        (None, "seleccione un archivo CSV"),
        (_upload("", b"10412345678"), "seleccione un archivo CSV"),
        (_upload("registros.txt", b"10412345678"), "extensión .csv"),
        (_upload("vacio.csv", b""), "está vacío"),
        (
            _upload("enorme.csv", b"0" * (MAX_CSV_UPLOAD_BYTES + 1)),
            "no puede superar los 10 MB",
        ),
    ],
)
async def test_csv_upload_rejects_unusable_files(
    upload: UploadFile | None, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        await read_csv_upload(upload)
