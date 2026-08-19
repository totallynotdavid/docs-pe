from __future__ import annotations

import re

from typing import TYPE_CHECKING

import pytest

from litestar.datastructures import UploadFile
from litestar.testing import TestClient
from portal.domain.errors import InputValidationError, Reason
from portal.messages import message_for
from portal.web.assets import COMPONENTS_DIR, STATIC_DIR, build_component_stylesheet
from portal.web.uploads import MAX_CSV_UPLOAD_BYTES, read_csv_upload


if TYPE_CHECKING:
    from litestar import Litestar


def _upload(name: str, content: bytes) -> UploadFile:
    return UploadFile(content_type="text/csv", filename=name, file_data=content)


def test_responses_use_same_origin_content_security_policy(app: Litestar) -> None:
    with TestClient(app) as client:
        policy = client.get("/login").headers["content-security-policy"]

    assert "default-src 'self'" in policy
    assert "unsafe-inline" not in policy
    assert "unsafe-eval" not in policy


def test_responses_deny_powerful_browser_features(app: Litestar) -> None:
    with TestClient(app) as client:
        headers = client.get("/login").headers

    assert headers["permissions-policy"] == (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    )
    assert headers["x-frame-options"] == "DENY"


def test_login_page_uses_available_local_assets(app: Litestar) -> None:
    with TestClient(app) as client:
        page = client.get("/login")

        assert re.search(r'(?:src|href)="(?:https?:)?//', page.text) is None

        references = set(re.findall(r'(?:src|href)="(/static/[^"]+)"', page.text))
        assert "/static/htmx.min.js" in references

        for reference in references:
            assert client.get(reference).status_code == 200, reference


def test_component_bundle_contains_every_component_stylesheet() -> None:
    url = build_component_stylesheet()
    bundled = (STATIC_DIR / url.removeprefix("/static/")).read_text()

    for stylesheet in COMPONENTS_DIR.glob("*.css"):
        assert stylesheet.read_text() in bundled, stylesheet.name


async def test_csv_upload_accepts_valid_file_and_strips_directories() -> None:
    name, content = await read_csv_upload(_upload("../../barranca.CSV", b"10412345678"))

    assert name == "barranca.CSV"
    assert content == b"10412345678"


@pytest.mark.parametrize(
    ("upload", "reason", "message"),
    [
        (None, Reason.CSV_REQUIRED, "seleccione un archivo CSV"),
        (
            _upload("", b"10412345678"),
            Reason.CSV_REQUIRED,
            "seleccione un archivo CSV",
        ),
        (
            _upload("registros.txt", b"10412345678"),
            Reason.CSV_EXTENSION,
            "seleccione un archivo con extensión .csv",
        ),
        (
            _upload("vacio.csv", b""),
            Reason.CSV_EMPTY,
            "el archivo CSV está vacío",
        ),
        (
            _upload("enorme.csv", b"0" * (MAX_CSV_UPLOAD_BYTES + 1)),
            Reason.CSV_TOO_LARGE,
            "el archivo CSV no puede superar los 15 MB",
        ),
    ],
)
async def test_csv_upload_rejects_invalid_files(
    upload: UploadFile | None,
    reason: Reason,
    message: str,
) -> None:
    with pytest.raises(InputValidationError) as raised:
        await read_csv_upload(upload)

    assert raised.value.reason is reason
    assert message_for(raised.value) == message
