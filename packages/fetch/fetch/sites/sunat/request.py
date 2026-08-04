from __future__ import annotations

import secrets


# SUNAT only checks that the token is present and has the expected 52-character
# base-36 shape.
_TOKEN_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
_TOKEN_LENGTH = 52


def random_token() -> str:
    return "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(_TOKEN_LENGTH))


def build_consulta_body(*, ruc: str, token: str) -> dict[str, str]:
    return {
        "accion": "consPorRuc",
        "razSoc": "",
        "nroRuc": ruc,
        "nrodoc": "",
        "token": token,
        "contexto": "ti-it",
        "modo": "1",
        "rbtnTipo": "1",
        "search1": ruc,
        "tipdoc": "1",
        "search2": "",
        "search3": "",
        "codigo": "",
    }
