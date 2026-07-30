-- Durable installation state and team-owned proxy credential lifecycle.

CREATE TABLE portal_installation_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    initial_team_id uuid REFERENCES portal_teams(id) ON DELETE RESTRICT,
    completed_by uuid REFERENCES portal_users(id) ON DELETE RESTRICT,
    completed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (
        (initial_team_id IS NULL AND completed_by IS NULL AND completed_at IS NULL)
        OR (initial_team_id IS NOT NULL AND completed_by IS NOT NULL AND completed_at IS NOT NULL)
    )
);
INSERT INTO portal_installation_state (singleton) VALUES (true)
ON CONFLICT (singleton) DO NOTHING;

ALTER TABLE portal_team_proxy_credential_versions
    ADD COLUMN lifecycle text,
    ADD COLUMN validated_at timestamptz,
    ADD COLUMN failure_detail text;

UPDATE portal_team_proxy_credential_versions
   SET lifecycle = CASE WHEN is_active THEN 'active' ELSE 'retired' END
 WHERE lifecycle IS NULL;

-- Raw legacy credentials cannot safely be assigned to either supported adapter.
-- Retire them before constraining the provider set; jobs retain their immutable
-- version reference, while new work requires a freshly validated configuration.
UPDATE portal_team_proxy_credential_versions
   SET provider = 'geonode', lifecycle = 'retired', is_active = false,
       failure_detail = 'La configuración anterior debe reemplazarse.'
 WHERE provider NOT IN ('geonode', 'dataimpulse');

ALTER TABLE portal_team_proxy_credential_versions
    ALTER COLUMN lifecycle SET NOT NULL,
    DROP CONSTRAINT IF EXISTS portal_team_proxy_credential_versions_provider_check,
    ADD CONSTRAINT portal_proxy_provider_supported
        CHECK (provider IN ('geonode', 'dataimpulse')),
    ADD CONSTRAINT portal_proxy_credential_lifecycle_valid
        CHECK (lifecycle IN ('draft', 'validating', 'active', 'failed', 'retired')),
    ADD CONSTRAINT portal_proxy_credential_active_consistent
        CHECK (is_active = (lifecycle = 'active'));

CREATE UNIQUE INDEX portal_active_credential_lifecycle_idx
    ON portal_team_proxy_credential_versions (credential_id)
    WHERE lifecycle = 'active';

CREATE TABLE portal_proxy_credential_events (
    id uuid PRIMARY KEY,
    credential_version_id uuid NOT NULL
        REFERENCES portal_team_proxy_credential_versions(id) ON DELETE RESTRICT,
    from_lifecycle text NOT NULL
        CHECK (from_lifecycle IN ('draft', 'validating', 'active', 'failed', 'retired')),
    to_lifecycle text NOT NULL
        CHECK (to_lifecycle IN ('draft', 'validating', 'active', 'failed', 'retired')),
    detail text NOT NULL CHECK (length(detail) <= 240),
    actor_id uuid REFERENCES portal_users(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX portal_proxy_credential_events_version_idx
    ON portal_proxy_credential_events (credential_version_id, created_at);

CREATE OR REPLACE FUNCTION portal_reject_proxy_credential_version_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.credential_id IS DISTINCT FROM OLD.credential_id
       OR NEW.team_id IS DISTINCT FROM OLD.team_id
       OR NEW.version IS DISTINCT FROM OLD.version
       OR NEW.provider IS DISTINCT FROM OLD.provider
       OR NEW.config_ciphertext IS DISTINCT FROM OLD.config_ciphertext
       OR NEW.key_id IS DISTINCT FROM OLD.key_id
       OR NEW.created_by IS DISTINCT FROM OLD.created_by
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'proxy credential versions are immutable';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER portal_proxy_credential_versions_immutable
    BEFORE UPDATE ON portal_team_proxy_credential_versions
    FOR EACH ROW EXECUTE FUNCTION portal_reject_proxy_credential_version_mutation();

-- The deferred trigger lets a single transaction insert a team and its first leader.
-- It locks the parent team row so two concurrent leader removals cannot both commit.
CREATE OR REPLACE FUNCTION portal_require_team_leader()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE selected_team uuid;
BEGIN
    IF TG_OP = 'DELETE' THEN
        selected_team := OLD.team_id;
    ELSE
        selected_team := NEW.team_id;
    END IF;
    PERFORM id FROM portal_teams WHERE id = selected_team FOR UPDATE;
    IF NOT FOUND THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM portal_team_memberships
         WHERE team_id = selected_team AND role = 'team_leader'
    ) THEN
        RAISE EXCEPTION 'a team must retain at least one leader'
            USING ERRCODE = '23514';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;
CREATE CONSTRAINT TRIGGER portal_team_must_have_leader
    AFTER INSERT OR UPDATE OF role OR DELETE ON portal_team_memberships
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION portal_require_team_leader();
