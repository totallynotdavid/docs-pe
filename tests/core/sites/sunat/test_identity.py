from __future__ import annotations

import json

import pytest

from core.domain.errors import ProviderSchemaError
from core.sites.sunat.identity import IdentityRecord, parse_identity


def test_extracts_and_strips_the_razon_social() -> None:
    payload = json.dumps({"lista": [{"apenomdenunciado": "  ACME SAC  "}]})
    assert parse_identity(payload) == IdentityRecord(razon_social="ACME SAC")


def test_confirmed_absent_when_error_is_present_and_lista_is_missing() -> None:
    assert parse_identity(json.dumps({"error": "no existe"})) is None


def test_both_lista_and_error_present_is_not_treated_as_confirmed_absence() -> None:
    # Both keys present is schema drift, not confirmed absence.
    payload = json.dumps({"lista": [], "error": "no existe"})
    with pytest.raises(ProviderSchemaError, match="missing the expected fields"):
        parse_identity(payload)


def test_neither_lista_nor_error_raises() -> None:
    with pytest.raises(ProviderSchemaError, match="no lista or error"):
        parse_identity(json.dumps({"something": "else"}))


def test_invalid_json_raises() -> None:
    with pytest.raises(ProviderSchemaError, match="not valid json"):
        parse_identity("not json")


def test_a_non_object_json_body_raises() -> None:
    with pytest.raises(ProviderSchemaError, match="not a json object"):
        parse_identity(json.dumps([1, 2, 3]))


def test_lista_missing_the_expected_field_raises() -> None:
    payload = json.dumps({"lista": [{"unexpected": "field"}]})
    with pytest.raises(ProviderSchemaError, match="missing the expected fields"):
        parse_identity(payload)


def test_an_empty_razon_social_raises() -> None:
    payload = json.dumps({"lista": [{"apenomdenunciado": "   "}]})
    with pytest.raises(ProviderSchemaError, match="empty razon social"):
        parse_identity(payload)
