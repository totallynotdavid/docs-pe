from __future__ import annotations

import secrets


# SUNAT's grecaptcha wrapper is a client-side stub that returns a random 52-char
# base-36 string; the server only checks the token is present and plausibly shaped,
# never that it is a real reCAPTCHA response, so this mints a token of the same shape.
_TOKEN_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
_TOKEN_LENGTH = 52


def random_token() -> str:
    return "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(_TOKEN_LENGTH))


def build_consulta_body(*, ruc: str, token: str) -> dict[str, str]:
    # The consPorRuc consulta: returns the ficha RUC HTML page (razon social, and
    # for a persona natural the "Tipo de Documento" record).
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


def build_reps_body(*, ruc: str, razon_social: str) -> dict[str, str]:
    # The getRepLeg consulta: returns the legal-representatives table for a company.
    # The server ignores the desRuc value (verified: an empty or wrong name still
    # returns the full table) but errors if the key is absent, so the parsed razon
    # social is passed to mirror the browser, not because it is load-bearing.
    return {
        "accion": "getRepLeg",
        "contexto": "ti-it",
        "modo": "1",
        "desRuc": razon_social,
        "nroRuc": ruc,
    }
