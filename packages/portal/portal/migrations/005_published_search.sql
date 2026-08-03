-- A RUC-10 contains its owner's DNI, so searches need substring matching.
-- Btree indexes cannot serve a leading wildcard.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX portal_job_items_document_trgm_idx
    ON portal_job_items USING gin (document gin_trgm_ops)
    WHERE state = 'published';
