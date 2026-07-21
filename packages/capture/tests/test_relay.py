from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from capture.relay import RelayState
from capture.ruc import RUC
from capture.sites.entel.site import ENTEL
from capture.store import ObservationStore


if TYPE_CHECKING:
    from pathlib import Path


RUC_A = "20100000092"
RUC_B = "20100000093"


def _state(store: ObservationStore) -> RelayState:
    return RelayState(
        rucs=[RUC(RUC_A), RUC(RUC_B)],
        store=store,
        run_id="run",
        token="token",
        site=ENTEL,
    )


def _debt_payload(ruc: str) -> dict[str, object]:
    return {
        "ruc": ruc,
        "hasError": False,
        "debt": {
            "DocumentNumber": ruc,
            "DebtTotal": "726.91",
            "HasPunishment": False,
        },
        "elapsedMs": 900,
        "mintMs": 300,
    }


def test_success_advances_and_records(tmp_path: Path) -> None:
    with ObservationStore(tmp_path / "state.sqlite3") as store:
        state = _state(store)
        state.record(_debt_payload(RUC_A))
        assert state.succeeded == 1
        assert state.index == 1
        assert store.latest("entel", RUC_A) == {
            "debt_total": "726.91",
            "has_punishment": "False",
        }


def test_reject_is_recorded_without_a_verified_row(tmp_path: Path) -> None:
    with ObservationStore(tmp_path / "state.sqlite3") as store:
        state = _state(store)
        payload = _debt_payload(RUC_A)
        payload["hasError"] = True
        state.record(payload)
        assert state.rejected == 1
        assert state.index == 1
        assert store.latest("entel", RUC_A) is None


def test_failure_is_recorded(tmp_path: Path) -> None:
    with ObservationStore(tmp_path / "state.sqlite3") as store:
        state = _state(store)
        state.record({"ruc": RUC_A, "exception": "boom"})
        assert state.failed == 1
        assert state.index == 1


def test_mismatched_ruc_is_rejected(tmp_path: Path) -> None:
    with ObservationStore(tmp_path / "state.sqlite3") as store:
        state = _state(store)
        with pytest.raises(ValueError, match="unexpected RUC"):
            state.record(_debt_payload(RUC_B))
        assert state.index == 0
