from __future__ import annotations

import uuid

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from jobs.service import Cancelled, Conflict, JobsService, PermissionDenied, utcnow
from jobs.settings import Settings


@dataclass
class TeamFixture:
    service: JobsService
    admin_id: str
    leader_id: str
    member_id: str
    team_id: str
    worker_token: str


@pytest.fixture
def team(tmp_path: Path) -> TeamFixture:
    worker_token = uuid.uuid4().hex
    session_secret = uuid.uuid4().hex
    bootstrap_password = uuid.uuid4().hex
    local_password = uuid.uuid4().hex
    service = JobsService(
        Settings(
            database_path=tmp_path / "jobs.sqlite3",
            object_root=tmp_path / "objects",
            session_secret=session_secret,
            bootstrap_admin_email="admin@example.test",
            bootstrap_admin_password=bootstrap_password,
            worker_bootstrap_token=worker_token,
        )
    )
    admin_id = service.bootstrap_admin()
    assert admin_id is not None
    leader_id = service.create_user(
        admin_id, email="leader@example.test", password=local_password
    )["id"]
    member_id = service.create_user(
        admin_id, email="member@example.test", password=local_password
    )["id"]
    team_id = service.create_team(admin_id, name="North")["id"]
    service.add_membership(admin_id, team_id=team_id, user_id=leader_id, role="leader")
    service.add_membership(admin_id, team_id=team_id, user_id=member_id, role="member")
    # No real provider account data is present and tests never run a source adapter.
    service.store_team_credential(
        admin_id,
        team_id=team_id,
        provider="geonode",
        secret_ref="test-only-reference",
        secret_json='{"credential_ref":"test-only-reference"}',
    )
    return TeamFixture(service, admin_id, leader_id, member_id, team_id, worker_token)


def submit_one(team: Any, content: bytes = b"12345678\n") -> str:
    return team.service.submit_job(
        team.leader_id,
        team_id=team.team_id,
        sources=["osiptel"],
        provider="geonode",
        input_bytes=content,
        idempotency_key=uuid.uuid4().hex,
    ).id


def claim_one(team: Any) -> dict[str, Any]:
    team.service.register_worker(
        worker_id="test-worker",
        bootstrap_token=team.worker_token,
        sources=["osiptel", "sunat", "sunat_reps"],
        capacity=1,
    )
    claimed = team.service.claim_work(
        worker_id="test-worker",
        worker_token=team.worker_token,
        max_items=1,
    )
    assert len(claimed) == 1
    return claimed[0]


def checkpoint_success(
    team: Any, lease: dict[str, Any], *, attempt_id: str | None = None
) -> dict[str, Any]:
    return team.service.checkpoint(
        worker_id="test-worker",
        worker_token=team.worker_token,
        lease_id=str(lease["lease_id"]),
        work_item_id=str(lease["work_item_id"]),
        fence=int(lease["fence"]),
        version=int(lease["version"]),
        attempt_id=attempt_id or uuid.uuid4().hex,
        sequence=1,
        outcome="succeeded",
        payload={"rows": [["postpaid", "***", "carrier"]]},
        healthy_contact_delta=1,
    )


def test_partial_validation_records_every_exclusion(team: Any) -> None:
    job_id = submit_one(team, b"1234567\ninvalid\n1234567\n")

    view = team.service.job_view(team.leader_id, job_id)

    assert view["summary"] == {
        "succeeded": 0,
        "not_found": 0,
        "excluded": 2,
        "exhausted_or_failed": 0,
        "cancelled": 0,
        "remaining": 1,
        "ready": 1,
        "leased": 0,
        "retry_wait": 0,
    }
    assert team.service.job_exclusions(team.leader_id, job_id) == [
        {"ordinal": 2, "reason": "invalid_document"},
        {"ordinal": 3, "reason": "duplicate_document"},
    ]
    assert team.service.job_view(team.member_id, job_id)["summary"]["excluded"] == 2
    with pytest.raises(PermissionDenied):
        team.service.job_exclusions(team.member_id, job_id)


def test_duplicate_checkpoint_is_idempotent_and_stale_checkpoint_is_fenced(
    team: Any,
) -> None:
    submit_one(team)
    lease = claim_one(team)
    attempt_id = uuid.uuid4().hex

    first = checkpoint_success(team, lease, attempt_id=attempt_id)
    duplicate = checkpoint_success(team, lease, attempt_id=attempt_id)

    assert first == {"accepted": True, "duplicate": False, "state": "succeeded"}
    assert duplicate == {"accepted": True, "duplicate": True}
    with pytest.raises(Conflict, match="stale"):
        checkpoint_success(team, lease, attempt_id=uuid.uuid4().hex)


def test_cancellation_stops_claims_and_rejects_late_checkpoint(team: Any) -> None:
    job_id = submit_one(team)
    lease = claim_one(team)

    summary = team.service.cancel_job(team.leader_id, job_id)
    assert summary["leased"] == 1
    assert (
        team.service.claim_work(
            worker_id="test-worker", worker_token=team.worker_token, max_items=1
        )
        == []
    )
    with pytest.raises(Cancelled):
        checkpoint_success(team, lease)

    view = team.service.job_view(team.leader_id, job_id)
    assert view["state"] == "cancelled"
    assert view["summary"]["cancelled"] == 1


def test_expired_lease_recovers_work_and_old_fence_cannot_write(team: Any) -> None:
    submit_one(team)
    lease = claim_one(team)
    now = utcnow() + timedelta(minutes=2)

    assert team.service.sweep_expired_leases(now=now) == 1
    replacement = team.service.claim_work(
        worker_id="test-worker", worker_token=team.worker_token, max_items=1, now=now
    )[0]
    assert replacement["fence"] > lease["fence"]
    with pytest.raises(Conflict):
        checkpoint_success(team, lease)
    assert checkpoint_success(team, replacement)["state"] == "succeeded"


def test_repeated_lease_expiry_exhausts_the_item(team: Any) -> None:
    job_id = submit_one(team)
    claim_one(team)
    now = utcnow()

    for recovery in range(3):
        now += timedelta(minutes=2)
        assert team.service.sweep_expired_leases(now=now) == 1
        if recovery < 2:
            team.service.claim_work(
                worker_id="test-worker",
                worker_token=team.worker_token,
                max_items=1,
                now=now,
            )[0]

    view = team.service.job_view(team.leader_id, job_id)
    assert view["state"] == "completed"
    assert view["summary"]["exhausted_or_failed"] == 1


def test_short_local_password_is_a_product_conflict(team: Any) -> None:
    with pytest.raises(Conflict, match="at least 12"):
        team.service.create_user(
            team.admin_id, email="short@example.test", password="short"
        )


def test_result_search_is_team_scoped_and_membership_revocation_is_immediate(
    team: Any,
) -> None:
    job_id = submit_one(team)
    checkpoint_success(team, claim_one(team))
    second_team = team.service.create_team(team.admin_id, name="South")["id"]
    team.service.add_membership(
        team.admin_id, team_id=second_team, user_id=team.member_id, role="member"
    )

    page = team.service.search_results(
        team.member_id, team_id=team.team_id, document="123"
    )
    assert len(page["items"]) == 1
    assert (
        team.service.search_results(
            team.member_id, team_id=second_team, document="123"
        )["items"]
        == []
    )
    team.service.remove_membership(
        team.admin_id, team_id=team.team_id, user_id=team.member_id
    )
    with pytest.raises(PermissionDenied):
        team.service.search_results(team.member_id, team_id=team.team_id)
    with pytest.raises(PermissionDenied):
        team.service.job_view(team.member_id, job_id)


def test_worker_capacity_rejects_malicious_max_items(team: Any) -> None:
    submit_one(team)
    team.service.register_worker(
        worker_id="capacity-worker",
        bootstrap_token=team.worker_token,
        sources=["osiptel"],
        capacity=1,
    )
    with pytest.raises(Conflict, match="exceeds registered worker capacity"):
        team.service.claim_work(
            worker_id="capacity-worker",
            worker_token=team.worker_token,
            max_items=2,
        )
    lease = team.service.claim_work(
        worker_id="capacity-worker",
        worker_token=team.worker_token,
        max_items=1,
    )[0]
    assert lease["lease_id"]
    assert (
        team.service.claim_work(
            worker_id="capacity-worker",
            worker_token=team.worker_token,
            max_items=1,
        )
        == []
    )
    with pytest.raises(Conflict, match="lease_seconds"):
        team.service.renew_lease(
            worker_id="capacity-worker",
            worker_token=team.worker_token,
            lease_id=str(lease["lease_id"]),
            fence=int(lease["fence"]),
            lease_seconds=301,
        )


def test_worker_identity_is_validated_and_active_duplicates_are_rejected(
    team: Any,
) -> None:
    with pytest.raises(Conflict, match="worker_id"):
        team.service.register_worker(
            worker_id="../impersonated",
            bootstrap_token=team.worker_token,
            sources=["osiptel"],
            capacity=1,
        )
    submit_one(team)
    team.service.register_worker(
        worker_id="duplicate-worker",
        bootstrap_token=team.worker_token,
        sources=["osiptel"],
        capacity=1,
    )
    team.service.claim_work(
        worker_id="duplicate-worker",
        worker_token=team.worker_token,
        max_items=1,
    )
    with pytest.raises(Conflict, match="active leases"):
        team.service.register_worker(
            worker_id="duplicate-worker",
            bootstrap_token=team.worker_token,
            sources=["osiptel"],
            capacity=1,
        )
    team.service.sweep_expired_leases(now=utcnow() + timedelta(minutes=2))
    with pytest.raises(Conflict, match="different capabilities"):
        team.service.register_worker(
            worker_id="duplicate-worker",
            bootstrap_token=team.worker_token,
            sources=["sunat"],
            capacity=1,
        )


def test_concurrent_workers_claim_distinct_items(team: Any) -> None:
    submit_one(team, b"12345678\n")
    submit_one(team, b"12345679\n")
    for worker_id in ("worker-a", "worker-b"):
        team.service.register_worker(
            worker_id=worker_id,
            bootstrap_token=team.worker_token,
            sources=["osiptel"],
            capacity=1,
        )

    def claim(worker_id: str) -> list[dict[str, Any]]:
        return team.service.claim_work(
            worker_id=worker_id,
            worker_token=team.worker_token,
            max_items=1,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        claimed = list(executor.map(claim, ("worker-a", "worker-b")))
    assert len({str(item["work_item_id"]) for items in claimed for item in items}) == 2


def test_expired_lease_cannot_release_a_credential(team: Any) -> None:
    submit_one(team)
    lease = claim_one(team)
    with pytest.raises(PermissionDenied, match="active credential"):
        team.service.lease_credential(
            worker_id="test-worker",
            worker_token=team.worker_token,
            lease_id=str(lease["lease_id"]),
            now=utcnow() + timedelta(minutes=2),
        )


def test_result_search_uses_cursor_pagination_and_keeps_team_scope(team: Any) -> None:
    submit_one(team, b"12345678\n")
    checkpoint_success(team, claim_one(team))
    submit_one(team, b"12345679\n")
    checkpoint_success(team, claim_one(team))

    first_page = team.service.search_results(
        team.member_id, team_id=team.team_id, limit=1
    )
    assert len(first_page["items"]) == 1
    assert first_page["next_cursor"]
    second_page = team.service.search_results(
        team.member_id,
        team_id=team.team_id,
        cursor=first_page["next_cursor"],
        limit=1,
    )
    assert len(second_page["items"]) == 1
    assert second_page["items"][0]["job_id"] != first_page["items"][0]["job_id"]

    other_team = team.service.create_team(team.admin_id, name="South")
    with pytest.raises(PermissionDenied):
        team.service.search_results(team.member_id, team_id=other_team["id"], limit=1)


def test_input_and_checkpoint_payload_limits(team: Any) -> None:
    with pytest.raises(Conflict, match="input file is too large"):
        submit_one(team, b"1" * (1_048_576 + 1))
    submit_one(team)
    lease = claim_one(team)
    with pytest.raises(Conflict, match="payload is too large"):
        team.service.checkpoint(
            worker_id="test-worker",
            worker_token=team.worker_token,
            lease_id=str(lease["lease_id"]),
            work_item_id=str(lease["work_item_id"]),
            fence=int(lease["fence"]),
            version=int(lease["version"]),
            attempt_id=uuid.uuid4().hex,
            sequence=1,
            outcome="succeeded",
            payload={"oversized": "x" * (64 * 1024)},
        )


def test_only_leaders_can_submit_and_credential_metadata_never_returns_ciphertext(
    team: Any,
) -> None:
    with pytest.raises(PermissionDenied):
        team.service.submit_job(
            team.member_id,
            team_id=team.team_id,
            sources=["osiptel"],
            provider="geonode",
            input_bytes=b"12345678\n",
            idempotency_key="member-cannot-submit",
        )
    metadata = team.service.credential_metadata(team.member_id, team.team_id)
    assert metadata == [
        {
            "provider": "geonode",
            "secret_ref": "test-only-reference",
            "created_at": metadata[0]["created_at"],
        }
    ]
    assert "ciphertext" not in str(metadata)


def test_all_stable_fetch_sources_are_selectable_without_browser(team: Any) -> None:
    submitted = team.service.submit_job(
        team.leader_id,
        team_id=team.team_id,
        sources=["osiptel", "sunat", "sunat_reps"],
        provider="geonode",
        input_bytes=b"12345678\n20123456789\n",
        idempotency_key="all-stable-sources",
    )
    assert set(team.service.job_view(team.leader_id, submitted.id)["sources"]) == {
        "osiptel",
        "sunat",
        "sunat_reps",
    }


def test_terminal_events_are_outboxed_and_export_is_immutable(team: Any) -> None:
    job_id = submit_one(team)
    checkpoint_success(team, claim_one(team))

    export = team.service.create_export(team.leader_id, job_id)
    export_bytes = team.service.read_export(team.member_id, export["id"])
    with team.service.database.connection() as connection:
        events = connection.execute(
            "SELECT channel,delivery_state FROM notification_outbox WHERE job_id=? ORDER BY channel",
            (job_id,),
        ).fetchall()

    assert export_bytes.startswith(b"source,document,status,payload\r\n")
    assert [(row["channel"], row["delivery_state"]) for row in events] == [
        ("email", "disabled"),
        ("in_app", "pending"),
        ("kapso_whatsapp", "disabled"),
    ]
