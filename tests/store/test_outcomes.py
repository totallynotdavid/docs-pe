from __future__ import annotations

from typing import TYPE_CHECKING

from robot.domain.types import RUC, Result, Status
from robot.store.outcomes import OutcomeCounts


if TYPE_CHECKING:
    from robot.domain.types import Row
    from robot.store.outcomes import OutcomeStore


def _success(site: str, ruc: str, rows: tuple[Row, ...]) -> Result:
    return Result(
        ruc=RUC(ruc),
        site=site,
        status=Status.OK,
        rows=rows,
        http_session_id="sess",
        proxy_id="proxy",
    )


def _failure(site: str, ruc: str, *, attempt: int, healthy: bool = True) -> Result:
    return Result(
        ruc=RUC(ruc),
        site=site,
        status=Status.FAILED,
        error_code="ban_signal",
        error_detail="blocked",
        made_healthy_contact=healthy,
        attempt=attempt,
    )


def test_success_projects_its_rows_and_marks_the_pair_done(store: OutcomeStore) -> None:
    store.record_success(_success("osiptel", "20100000001", (("CLARO", 2, 2),)))
    assert list(store.success_rows("osiptel")) == [("20100000001", (("CLARO", 2, 2),))]
    assert ("osiptel", "20100000001") in store.done_pairs()
    assert store.counts("osiptel").succeeded == 1


def test_a_fresh_failure_is_retryable_not_done(store: OutcomeStore) -> None:
    store.record_failure(_failure("osiptel", "20100000001", attempt=4))
    assert ("osiptel", "20100000001") not in store.done_pairs(retry_cap=12)
    assert store.counts("osiptel", retry_cap=12) == OutcomeCounts(
        succeeded=0, terminal_failed=0, retryable=1
    )


def test_failed_attempts_accumulate_across_records(store: OutcomeStore) -> None:
    store.record_failure(_failure("osiptel", "20100000001", attempt=4))
    store.record_failure(_failure("osiptel", "20100000001", attempt=4))
    attempt_column = next(iter(store.error_rows("osiptel")))[3]
    assert attempt_column == "8"


def test_a_pair_retires_once_attempts_reach_the_cap(store: OutcomeStore) -> None:
    store.record_failure(_failure("osiptel", "20100000001", attempt=4))
    store.record_failure(_failure("osiptel", "20100000001", attempt=4))
    assert ("osiptel", "20100000001") in store.done_pairs(retry_cap=8)
    counts = store.counts("osiptel", retry_cap=8)
    assert counts.terminal_failed == 1
    assert counts.retryable == 0


def test_unhealthy_contact_does_not_count_toward_the_cap(store: OutcomeStore) -> None:
    # A failure recorded while the breaker was open is real (it is stored) but must
    # not push the pair toward retirement, or an outage could grind the backlog out.
    store.record_failure(_failure("osiptel", "20100000001", attempt=4, healthy=False))
    assert next(iter(store.error_rows("osiptel")))[3] == "0"
    assert ("osiptel", "20100000001") not in store.done_pairs(retry_cap=4)


def test_success_is_never_downgraded_by_a_late_failure(store: OutcomeStore) -> None:
    ruc = "20100000001"
    store.record_success(_success("osiptel", ruc, (("CLARO", 1, 1),)))
    store.record_failure(_failure("osiptel", ruc, attempt=4))
    assert list(store.success_rows("osiptel")) == [(ruc, (("CLARO", 1, 1),))]
    assert list(store.error_rows("osiptel")) == []
    assert store.counts("osiptel").succeeded == 1


def test_success_after_failure_clears_the_error(store: OutcomeStore) -> None:
    ruc = "20100000001"
    store.record_failure(_failure("osiptel", ruc, attempt=4))
    store.record_success(_success("osiptel", ruc, (("CLARO", 1, 1),)))
    assert list(store.error_rows("osiptel")) == []
    assert list(store.success_rows("osiptel")) == [(ruc, (("CLARO", 1, 1),))]


def test_an_empty_payload_is_an_honest_success_with_no_projected_rows(
    store: OutcomeStore,
) -> None:
    store.record_success(_success("sunat", "20100000001", ()))
    assert list(store.success_rows("sunat")) == [("20100000001", ())]
    assert ("sunat", "20100000001") in store.done_pairs()


def test_the_same_ruc_is_tracked_independently_per_site(store: OutcomeStore) -> None:
    store.record_success(_success("osiptel", "20100000001", (("CLARO", 1, 1),)))
    store.record_failure(_failure("sunat", "20100000001", attempt=4))
    assert store.counts("osiptel").succeeded == 1
    assert store.counts("sunat").retryable == 1
    assert ("osiptel", "20100000001") in store.done_pairs()
    assert ("sunat", "20100000001") not in store.done_pairs()
