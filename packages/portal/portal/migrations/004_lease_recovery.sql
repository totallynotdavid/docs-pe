-- Recovery for items whose worker died while holding the lease.

-- Counted when an item is handed to a worker, so it is the retry count by
-- construction. The lease sweep reads it to decide between retry and failure.
ALTER TABLE portal_job_items
    ADD COLUMN attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0);

-- An item already leased when this migration runs has been handed out once.
UPDATE portal_job_items SET attempts = 1 WHERE state = 'running';
