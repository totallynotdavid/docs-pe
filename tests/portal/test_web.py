from __future__ import annotations

import io
import re

from typing import TYPE_CHECKING

import pytest

from fastapi import UploadFile
from fastapi.testclient import TestClient
from portal.domain.errors import InputValidationError, Reason
from portal.messages import message_for
from portal.web.render import COMPONENTS_DIR, PAGES_DIR
from portal.web.uploads import MAX_CSV_UPLOAD_BYTES, read_csv_upload


if TYPE_CHECKING:
    from fastapi import FastAPI


def _upload(name: str, content: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(content), filename=name)


def test_every_response_carries_a_policy_that_trusts_only_this_origin(
    app: FastAPI,
) -> None:
    with TestClient(app) as client:
        policy = client.get("/login").headers["content-security-policy"]

    assert "default-src 'self'" in policy
    assert "unsafe-inline" not in policy
    assert "unsafe-eval" not in policy


def test_the_portal_serves_every_asset_a_page_asks_for(app: FastAPI) -> None:
    with TestClient(app) as client:
        page = client.get("/login")

        assert re.search(r'(?:src|href)="(?:https?:)?//', page.text) is None

        references = set(re.findall(r'(?:src|href)="(/estatico/[^"]+)"', page.text))

        assert {
            "/estatico/htmx.min.js",
            "/estatico/htmx-ext-sse.min.js",
        } <= references

        for reference in references:
            assert client.get(reference).status_code == 200, reference


def _declared_stylesheets(source: str) -> set[str]:
    return {
        name.strip()
        for declaration in re.findall(r"\{#css(.*?)#\}", source, re.DOTALL)
        for name in declaration.split(",")
        if name.strip()
    }


def test_the_layout_carries_the_styles_htmx_can_swap_in_later() -> None:
    # Fragment styles must be present before HTMX swaps the fragment in.
    layout = (COMPONENTS_DIR / "Layout.jinja").read_text()
    covered = _declared_stylesheets(layout)

    for fragment in sorted(PAGES_DIR.glob("*Fragment.jinja")):
        tags = set(
            re.findall(
                r"<([A-Z][A-Za-z0-9]*)[\s/>]",
                fragment.read_text(),
            )
        )

        for tag in tags:
            component = COMPONENTS_DIR / f"{tag}.jinja"
            required = _declared_stylesheets(component.read_text())

            if (COMPONENTS_DIR / f"{tag}.css").exists():
                required.add(f"{tag}.css")

            assert required <= covered, f"{fragment.name} -> {tag}"


def test_every_component_stylesheet_is_reachable_but_no_template_is(
    app: FastAPI,
) -> None:
    with TestClient(app) as client:
        for stylesheet in sorted(COMPONENTS_DIR.glob("*.css")):
            served = client.get(f"/estatico/componentes/{stylesheet.name}")

            assert served.status_code == 200, stylesheet.name
            assert served.headers["content-type"].startswith("text/css")

        assert client.get("/estatico/componentes/Layout.jinja").status_code == 404


async def test_csv_upload_accepts_a_valid_file_and_strips_its_directories() -> None:
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
            "el archivo CSV no puede superar los 10 MB",
        ),
    ],
)
async def test_csv_upload_rejects_unusable_files(
    upload: UploadFile | None,
    reason: Reason,
    message: str,
) -> None:
    with pytest.raises(InputValidationError) as raised:
        await read_csv_upload(upload)

    assert raised.value.reason is reason
    assert message_for(raised.value) == message
