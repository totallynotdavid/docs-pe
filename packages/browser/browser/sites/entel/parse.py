from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, NoReturn

from browser.errors import BrowserError, RejectedError
from browser.result import LookupResult


def parse_lookup_result(payload: object, *, expected_ruc: str) -> LookupResult:
    if not isinstance(payload, dict):
        _fail("Entel lookup returned an unsupported payload")
    if payload.get("exception"):
        msg = f"Entel lookup script failed: {payload['exception']}"
        raise BrowserError(msg)
    if payload.get("ruc") != expected_ruc:
        _fail("Entel lookup returned a response for another RUC")
    if payload.get("hasError") is True:
        msg = f"Entel rejected lookup for RUC {expected_ruc}"
        raise RejectedError(msg)
    if payload.get("hasError") is not False:
        _fail("Entel lookup omitted HasErrorDebt")
    debt = payload.get("debt")
    if not isinstance(debt, dict):
        _fail("Entel lookup omitted debt data")
    if debt.get("DocumentNumber") != expected_ruc:
        _fail("Entel debt data contains another document number")
    total, has_punishment = _parse_debt_fields(debt)
    return LookupResult(
        ruc=expected_ruc,
        columns={
            "debt_total": f"{total:.2f}",
            "has_punishment": str(has_punishment),
        },
        elapsed_ms=_nonnegative_int(payload.get("elapsedMs")),
        mint_ms=_nonnegative_int(payload.get("mintMs")),
    )


def _parse_debt_fields(debt: dict[str, Any]) -> tuple[Decimal, bool]:
    try:
        total = Decimal(str(debt["DebtTotal"]))
    except (InvalidOperation, KeyError) as exc:
        msg = "Entel debt total is invalid"
        raise BrowserError(msg) from exc
    if not total.is_finite() or total < 0:
        _fail("Entel debt total is invalid")
    has_punishment = debt.get("HasPunishment")
    if not isinstance(has_punishment, bool):
        _fail("Entel debt data omitted HasPunishment")
    return total, has_punishment


def _nonnegative_int(value: object) -> int:
    if isinstance(value, int) and value >= 0:
        return value
    return 0


def _fail(message: str) -> NoReturn:
    raise BrowserError(message)
