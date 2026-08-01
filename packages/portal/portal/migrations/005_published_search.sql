-- Indexed substring search over published documents.

-- Documents are DNIs and RUCs, and a RUC-10 embeds its owner's DNI, so searching
-- a DNI must still find the RUC that contains it. A btree cannot serve a leading
-- wildcard, so the substring match needs a trigram index.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX portal_job_items_document_trgm_idx
    ON portal_job_items USING gin (document gin_trgm_ops)
    WHERE state = 'published';
