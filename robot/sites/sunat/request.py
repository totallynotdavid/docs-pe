from __future__ import annotations

import secrets


# SUNAT's grecaptcha wrapper is a client-side stub that returns a random 52-char
# base-36 string; the server only checks the token is present and plausibly shaped,
# never that it is a real reCAPTCHA response, so this mints a token of the same shape.
_TOKEN_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
_TOKEN_LENGTH = 52


def random_token() -> str:
    return "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(_TOKEN_LENGTH))


def build_body(*, ruc: str, token: str) -> dict[str, str]:
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
