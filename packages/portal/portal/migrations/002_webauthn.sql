-- Adds passkeys as a second factor alongside TOTP, and a pending-promotion
-- state so a user finishes their own enrollment instead of an operator or
-- promoting admin ever seeing the secret.

ALTER TABLE portal_users
    ADD COLUMN pending_site_admin boolean NOT NULL DEFAULT false,
    ADD CONSTRAINT portal_pending_site_admin_not_admin
        CHECK (NOT (is_site_admin AND pending_site_admin));

-- credential_id is the natural key: a discoverable login has no user_id yet,
-- only the assertion's credential id, and it must resolve to exactly one row
-- across every user in the installation.
CREATE TABLE portal_webauthn_credentials (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES portal_users(id) ON DELETE CASCADE,
    credential_id bytea NOT NULL UNIQUE,
    public_key bytea NOT NULL,
    sign_count bigint NOT NULL DEFAULT 0 CHECK (sign_count >= 0),
    transports text[] NOT NULL DEFAULT '{}',
    label text NOT NULL CHECK (length(trim(label)) > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz
);

CREATE INDEX portal_webauthn_credentials_user_idx
    ON portal_webauthn_credentials (user_id);

-- Registration/authentication challenges and pending (unconfirmed) TOTP
-- secrets are single-use and short-lived, exactly what portal_ephemeral's
-- OneTimeTokens already are for pending-mfa login tokens, so they live there
-- instead of a bespoke table.

-- portal_site_admin_requires_mfa only knew about the mfa_enabled column. A
-- factor can now also be a row in portal_webauthn_credentials, which a single
-- CHECK constraint on portal_users cannot see, so the invariant moves to a
-- constraint trigger, mirroring portal_require_site_admin below.
ALTER TABLE portal_users
    DROP CONSTRAINT portal_site_admin_requires_mfa;

CREATE OR REPLACE FUNCTION portal_require_admin_second_factor()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    affected_user uuid;
BEGIN
    IF TG_TABLE_NAME = 'portal_webauthn_credentials' THEN
        affected_user := OLD.user_id;
    ELSE
        affected_user := NEW.id;
    END IF;

    PERFORM 1 FROM portal_users WHERE id = affected_user AND is_site_admin FOR UPDATE;

    IF FOUND AND NOT EXISTS (
        SELECT 1 FROM portal_users WHERE id = affected_user AND mfa_enabled
    ) AND NOT EXISTS (
        SELECT 1 FROM portal_webauthn_credentials WHERE user_id = affected_user
    ) THEN
        RAISE EXCEPTION 'a site administrator must retain at least one second factor'
            USING ERRCODE = '23514';
    END IF;

    IF TG_TABLE_NAME = 'portal_webauthn_credentials' THEN
        RETURN OLD;
    END IF;

    RETURN NEW;
END;
$$;

CREATE CONSTRAINT TRIGGER portal_admin_requires_second_factor
    AFTER INSERT OR UPDATE OF is_site_admin, mfa_enabled
    ON portal_users
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    EXECUTE FUNCTION portal_require_admin_second_factor();

CREATE CONSTRAINT TRIGGER portal_admin_requires_second_factor_on_passkey_removal
    AFTER DELETE
    ON portal_webauthn_credentials
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    EXECUTE FUNCTION portal_require_admin_second_factor();
