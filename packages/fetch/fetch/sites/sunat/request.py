from __future__ import annotations

import secrets


# SUNAT's grecaptcha wrapper is a client-side stub returning a random 52-char base-36
# string, and the server only checks that a token is present and plausibly shaped. So
# this mints one of the same shape.
_TOKEN_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
_TOKEN_LENGTH = 52


def random_token() -> str:
    return "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(_TOKEN_LENGTH))


def build_consulta_body(*, ruc: str, token: str) -> dict[str, str]:
    # The consPorRuc consulta: returns the ficha RUC HTML page, used here only for
    # the persona natural "Tipo de Documento" record.
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
