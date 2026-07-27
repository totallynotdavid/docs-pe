from __future__ import annotations

import uuid

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


def test_result_search_is_team_scoped_and_membership_revocation_is_immediate(
    team: Any,
) -> None:
    job_id = submit_one(team)
    checkpoint_success(team, claim_one(team))
    second_team = team.service.create_team(team.admin_id, name="South")["id"]
    team.service.add_membership(
        team.admin_id, team_id=second_team, user_id=team.member_id, role="member"
    )

    assert (
        len(
            team.service.search_results(
                team.member_id, team_id=team.team_id, document="123"
            )
        )
        == 1
    )
    assert (
        team.service.search_results(team.member_id, team_id=second_team, document="123")
        == []
    )
    team.service.remove_membership(
        team.admin_id, team_id=team.team_id, user_id=team.member_id
    )
    with pytest.raises(PermissionDenied):
        team.service.search_results(team.member_id, team_id=team.team_id)
    with pytest.raises(PermissionDenied):
        team.service.job_view(team.member_id, job_id)


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
