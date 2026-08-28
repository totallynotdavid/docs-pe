from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cli.store.outcomes import OutcomeStore


if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def store(tmp_path: Path) -> Iterator[OutcomeStore]:
    with OutcomeStore(tmp_path / "run.state.sqlite3") as opened:
        yield opened
