-- Incremented when an item is leased to a worker.
ALTER TABLE portal_job_items
    ADD COLUMN attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0);

-- Existing running items have already been leased once.
UPDATE portal_job_items
SET attempts = 1
WHERE state = 'running';
