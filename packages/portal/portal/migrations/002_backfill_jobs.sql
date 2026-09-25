-- Historical `results/` scans never went through a submission, so origin marks
-- their jobs as backfill. copied_from_job_id points a team's copy of an
-- imported batch at the master batch it was assigned from.
ALTER TABLE portal_jobs
    ADD COLUMN origin text NOT NULL DEFAULT 'submission'
        CONSTRAINT portal_jobs_origin_known
        CHECK (origin IN ('submission', 'backfill')),
    ADD COLUMN copied_from_job_id uuid REFERENCES portal_jobs(id);

CREATE INDEX portal_jobs_backfill_masters_idx
    ON portal_jobs (created_at DESC)
    WHERE origin = 'backfill' AND copied_from_job_id IS NULL;
