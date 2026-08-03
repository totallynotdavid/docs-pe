from __future__ import annotations

from tests.paths import REPO_ROOT


def test_postgresql_schema_captures_queue_and_durable_boundaries() -> None:
    migration = (
        REPO_ROOT / "packages/portal/portal/migrations/001_portal_foundation.sql"
    )
    sql = migration.read_text(encoding="utf-8")

    for table in (
        "portal_sites",
        "portal_users",
        "portal_sessions",
        "portal_teams",
        "portal_team_memberships",
        "portal_team_proxy_credential_versions",
        "portal_proxy_credential_events",
        "portal_installation_state",
        "portal_jobs",
        "portal_job_items",
        "portal_job_events",
        "portal_notification_outbox",
        "portal_notification_deliveries",
        "portal_object_references",
    ):
        assert f"CREATE TABLE {table}" in sql
    assert "FOR UPDATE" in sql
    assert "max_active_jobs = 5" in sql
    assert "portal_object_references_immutable" in sql
    assert "kapso_whatsapp" in sql
    assert "portal_check_sources_known" in sql
    assert "portal_set_updated_at" in sql
