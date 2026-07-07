from __future__ import annotations

import re

from robot.sites.sunat.request import build_consulta_body, build_reps_body, random_token


_TOKEN_RE = re.compile(r"^[0-9a-z]{52}$")


def test_random_token_is_52_base36_characters() -> None:
    # The server only checks the token is present and plausibly shaped (see
    # request.py), so this shape is the one thing that actually matters.
    assert _TOKEN_RE.match(random_token())


def test_random_token_is_not_constant() -> None:
    assert random_token() != random_token()


def test_consulta_body_carries_the_ruc_and_token_in_the_fields_sunat_reads() -> None:
    body = build_consulta_body(ruc="20100000001", token="tok")
    assert body["nroRuc"] == "20100000001"
    assert body["search1"] == "20100000001"
    assert body["token"] == "tok"


def test_reps_body_carries_the_ruc_and_razon_social() -> None:
    body = build_reps_body(ruc="20100000001", razon_social="ACME SAC")
    assert body["accion"] == "getRepLeg"
    assert body["nroRuc"] == "20100000001"
    assert body["desRuc"] == "ACME SAC"
