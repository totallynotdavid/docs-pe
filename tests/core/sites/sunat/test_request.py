from __future__ import annotations

import re

from core.sites.sunat.request import build_consulta_body, random_token


_TOKEN_RE = re.compile(r"^[0-9a-z]{52}$")


def test_random_token_is_52_base36_characters() -> None:
    # Server does not validate entropy, only shape.
    assert _TOKEN_RE.match(random_token())


def test_random_token_is_not_constant() -> None:
    # Repeated requests must produce more than one token.
    tokens = {random_token() for _ in range(500)}
    assert len(tokens) > 1


def test_consulta_body_carries_the_ruc_and_token_in_the_fields_sunat_reads() -> None:
    body = build_consulta_body(ruc="20100000001", token="tok")
    assert body == {
        "accion": "consPorRuc",
        "razSoc": "",
        "nroRuc": "20100000001",
        "nrodoc": "",
        "token": "tok",
        "contexto": "ti-it",
        "modo": "1",
        "rbtnTipo": "1",
        "search1": "20100000001",
        "tipdoc": "1",
        "search2": "",
        "search3": "",
        "codigo": "",
    }
