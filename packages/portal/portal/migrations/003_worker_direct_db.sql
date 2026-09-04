-- Wake idle workers when a job becomes running or an item returns to pending.
-- Polling remains necessary when a breaker reopens without a row change.
CREATE OR REPLACE FUNCTION portal_notify_work_available()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM pg_notify('portal_work_available', '');
    RETURN NULL;
END;
$$;

-- INSERT and UPDATE use separate triggers because INSERT WHEN clauses cannot
-- reference OLD.
CREATE TRIGGER portal_jobs_notify_running_insert
    AFTER INSERT ON portal_jobs
    FOR EACH ROW
    WHEN (NEW.state = 'running')
    EXECUTE FUNCTION portal_notify_work_available();

CREATE TRIGGER portal_jobs_notify_running_update
    AFTER UPDATE OF state ON portal_jobs
    FOR EACH ROW
    WHEN (NEW.state = 'running' AND OLD.state IS DISTINCT FROM NEW.state)
    EXECUTE FUNCTION portal_notify_work_available();

-- Job insertion already wakes the dispatcher, so this only covers lease
-- expiry returning an item to pending.
CREATE TRIGGER portal_job_items_notify_pending
    AFTER UPDATE OF state ON portal_job_items
    FOR EACH ROW
    WHEN (NEW.state = 'pending' AND OLD.state IS DISTINCT FROM NEW.state)
    EXECUTE FUNCTION portal_notify_work_available();

-- Grant workers only the queue, lease, heartbeat, slot, and result metadata
-- operations they perform directly. Credential ciphertext is not readable by
-- the worker role; worker-api performs the audited decryption.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'portal_worker_base') THEN
        CREATE ROLE portal_worker_base NOLOGIN;
    END IF;
END
$$;

GRANT SELECT, UPDATE ON portal_job_items TO portal_worker_base;
GRANT SELECT, UPDATE ON portal_jobs TO portal_worker_base;
-- SELECT ... FOR UPDATE requires UPDATE privilege even when no column changes.
GRANT SELECT, UPDATE ON portal_queue_control TO portal_worker_base;
GRANT SELECT (id, provider) ON portal_team_proxy_credential_versions TO portal_worker_base;
GRANT SELECT, INSERT, UPDATE ON portal_circuit_breakers TO portal_worker_base;
-- ON CONFLICT DO UPDATE requires SELECT in addition to INSERT and UPDATE.
GRANT SELECT, INSERT, UPDATE ON portal_entries TO portal_worker_base;
GRANT INSERT ON portal_lookup_attempts TO portal_worker_base;
GRANT INSERT ON portal_object_references TO portal_worker_base;
GRANT SELECT, UPDATE ON portal_proxy_slots TO portal_worker_base;
GRANT INSERT ON portal_job_events TO portal_worker_base;
-- Terminal job transitions write notification rows here.
GRANT INSERT ON portal_notification_outbox TO portal_worker_base;
-- UPDATE ... WHERE worker_id requires SELECT on the predicate column.
GRANT SELECT (worker_id) ON portal_workers TO portal_worker_base;
GRANT UPDATE (last_seen_at, cpu_percent, memory_mb, current_job_id)
    ON portal_workers TO portal_worker_base;

-- Per-node LOGIN roles are created during enrollment and receive these grants.
