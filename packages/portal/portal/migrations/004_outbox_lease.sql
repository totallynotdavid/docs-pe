-- A claimed row stays in 'sending' with available_at set to the lease expiry,
-- so the claim query takes 'sending' rows once their lease has run out. The
-- index covers both states. Rows already stuck in 'sending' have an
-- available_at in the past and are reclaimed by the next dispatch.
DROP INDEX portal_notification_outbox_claim_idx;

CREATE INDEX portal_notification_outbox_claim_idx
    ON portal_notification_outbox (available_at, id)
    WHERE state IN ('pending', 'sending');
