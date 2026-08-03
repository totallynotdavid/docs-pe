-- The set of known sites is a lookup table rather than an array literal
-- copied into every CHECK that needs it, so fetch.sites.registry.STABLE_SITES
-- has exactly one place to mirror when a site is added.
CREATE TABLE portal_sites (
    code text PRIMARY KEY
);

INSERT INTO portal_sites (code) VALUES ('osiptel'), ('sunat'), ('sunat_reps');

CREATE TABLE portal_users (
    id uuid PRIMARY KEY,
    email text NOT NULL UNIQUE CHECK (email = lower(email)),
    password_hash text NOT NULL,
    is_site_admin boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE portal_sessions (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES portal_users(id) ON DELETE CASCADE,
    token_hash text NOT NULL UNIQUE,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX portal_sessions_expiry_idx ON portal_sessions (expires_at);

CREATE TABLE portal_teams (
    id uuid PRIMARY KEY,
    slug text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
    name text NOT NULL CHECK (length(trim(name)) > 0),
    created_by uuid NOT NULL REFERENCES portal_users(id),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE portal_team_memberships (
    team_id uuid NOT NULL REFERENCES portal_teams(id) ON DELETE CASCADE,
    user_id uuid NOT NULL REFERENCES portal_users(id) ON DELETE CASCADE,
    role text NOT NULL CHECK (role IN ('team_leader', 'team_member')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (team_id, user_id)
);
CREATE INDEX portal_memberships_user_idx ON portal_team_memberships (user_id, team_id);

-- Deferred validation allows a team and its first leader in one transaction.
-- Locking the team prevents concurrent removals from deleting every leader.
CREATE OR REPLACE FUNCTION portal_require_team_leader()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    selected_team uuid;
BEGIN
    IF TG_OP = 'DELETE' THEN
        selected_team := OLD.team_id;
    ELSE
        selected_team := NEW.team_id;
    END IF;

    PERFORM id
      FROM portal_teams
     WHERE id = selected_team
     FOR UPDATE;

    IF NOT FOUND THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;

        RETURN NEW;
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM portal_team_memberships
         WHERE team_id = selected_team
           AND role = 'team_leader'
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
    AFTER INSERT OR UPDATE OF role OR DELETE
    ON portal_team_memberships
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    EXECUTE FUNCTION portal_require_team_leader();

CREATE TABLE portal_object_references (
    id uuid PRIMARY KEY,
    team_id uuid NOT NULL REFERENCES portal_teams(id) ON DELETE RESTRICT,
    provider text NOT NULL CHECK (length(trim(provider)) > 0),
    container text NOT NULL CHECK (length(trim(container)) > 0),
    object_key text NOT NULL CHECK (length(trim(object_key)) > 0),
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    content_type text NOT NULL CHECK (length(trim(content_type)) > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (team_id, provider, container, object_key, sha256),
    UNIQUE (id, team_id)
);

CREATE OR REPLACE FUNCTION portal_reject_object_reference_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'portal object references are immutable';
END;
$$;

CREATE TRIGGER portal_object_references_immutable
    BEFORE UPDATE OR DELETE ON portal_object_references
    FOR EACH ROW EXECUTE FUNCTION portal_reject_object_reference_mutation();

CREATE TABLE portal_team_proxy_credentials (
    id uuid PRIMARY KEY,
    team_id uuid NOT NULL REFERENCES portal_teams(id) ON DELETE CASCADE,
    label text NOT NULL CHECK (length(trim(label)) > 0),
    created_by uuid NOT NULL REFERENCES portal_users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    retired_at timestamptz,
    UNIQUE (team_id, label),
    UNIQUE (id, team_id)
);

CREATE TABLE portal_team_proxy_credential_versions (
    id uuid PRIMARY KEY,
    credential_id uuid NOT NULL,
    team_id uuid NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    provider text NOT NULL CONSTRAINT portal_proxy_provider_supported
        CHECK (provider IN ('geonode', 'dataimpulse')),
    config_ciphertext bytea NOT NULL CHECK (octet_length(config_ciphertext) > 0),
    key_id text NOT NULL CHECK (length(trim(key_id)) > 0),

    -- is_active mirrors lifecycle = 'active' so a filter can stay a plain
    -- boolean column; the CHECK below is what keeps the two from drifting.
    is_active boolean NOT NULL DEFAULT false,
    lifecycle text NOT NULL CONSTRAINT portal_proxy_credential_lifecycle_valid
        CHECK (
            lifecycle IN ('draft', 'validating', 'active', 'failed', 'retired')
        ),
    CONSTRAINT portal_proxy_credential_active_consistent
        CHECK (is_active = (lifecycle = 'active')),
    validated_at timestamptz,
    -- Not rendered anywhere yet. If a route ever displays it, the wording
    -- belongs in messages.py, the one place portal Spanish text is written.
    failure_detail text,

    created_by uuid NOT NULL REFERENCES portal_users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (credential_id, team_id)
        REFERENCES portal_team_proxy_credentials (id, team_id) ON DELETE CASCADE,
    UNIQUE (credential_id, version),
    UNIQUE (id, team_id)
);

CREATE UNIQUE INDEX portal_active_credential_lifecycle_idx
    ON portal_team_proxy_credential_versions (credential_id)
    WHERE lifecycle = 'active';

CREATE TABLE portal_proxy_credential_events (
    id uuid PRIMARY KEY,
    credential_version_id uuid NOT NULL
        REFERENCES portal_team_proxy_credential_versions(id)
        ON DELETE RESTRICT,
    from_lifecycle text NOT NULL CHECK (
        from_lifecycle IN ('draft', 'validating', 'active', 'failed', 'retired')
    ),
    to_lifecycle text NOT NULL CHECK (
        to_lifecycle IN ('draft', 'validating', 'active', 'failed', 'retired')
    ),
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
    FOR EACH ROW
    EXECUTE FUNCTION portal_reject_proxy_credential_version_mutation();

-- Shared by every table below that carries updated_at, so the column reflects
-- the last write regardless of which statement performed it.
CREATE OR REPLACE FUNCTION portal_set_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

CREATE TABLE portal_installation_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    initial_team_id uuid REFERENCES portal_teams(id) ON DELETE RESTRICT,
    completed_by uuid REFERENCES portal_users(id) ON DELETE RESTRICT,
    completed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (
        (
            initial_team_id IS NULL
            AND completed_by IS NULL
            AND completed_at IS NULL
        )
        OR (
            initial_team_id IS NOT NULL
            AND completed_by IS NOT NULL
            AND completed_at IS NOT NULL
        )
    )
);

INSERT INTO portal_installation_state (singleton) VALUES (true);

CREATE TRIGGER portal_installation_state_set_updated_at
    BEFORE UPDATE ON portal_installation_state
    FOR EACH ROW EXECUTE FUNCTION portal_set_updated_at();

CREATE TABLE portal_queue_control (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),

    -- Global concurrent job limit.
    max_active_jobs smallint NOT NULL CHECK (max_active_jobs = 5)
);

INSERT INTO portal_queue_control (singleton, max_active_jobs)
VALUES (true, 5);

-- SQL entry point for the queue mutex. Repositories perform the same
-- SELECT ... FOR UPDATE directly.
CREATE OR REPLACE FUNCTION portal_lock_queue_control()
RETURNS smallint LANGUAGE plpgsql AS $$
DECLARE maximum smallint;
BEGIN
    SELECT max_active_jobs
      INTO maximum
      FROM portal_queue_control
     WHERE singleton = true
     FOR UPDATE;

    RETURN maximum;
END;
$$;

-- A foreign key cannot constrain array elements, so an array column's site
-- membership is checked here against portal_sites instead of a copied literal.
CREATE OR REPLACE FUNCTION portal_check_sources_known()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT (NEW.sources <@ (SELECT array_agg(code) FROM portal_sites)) THEN
        RAISE EXCEPTION 'unknown source in %', NEW.sources;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TABLE portal_jobs (
    id uuid PRIMARY KEY,
    team_id uuid NOT NULL REFERENCES portal_teams(id) ON DELETE RESTRICT,
    submitted_by uuid NOT NULL REFERENCES portal_users(id),

    credential_version_id uuid NOT NULL,
    input_object_id uuid NOT NULL,

    filename text NOT NULL CHECK (length(trim(filename)) > 0),

    sources text[] NOT NULL CHECK (cardinality(sources) > 0),

    state text NOT NULL CHECK (
        state IN (
            'queued',
            'running',
            'cancelling',
            'completed',
            'failed',
            'cancelled'
        )
    ),

    queue_sequence bigint GENERATED ALWAYS AS IDENTITY UNIQUE,

    lease_owner text,
    lease_fence bigint NOT NULL DEFAULT 0 CHECK (lease_fence >= 0),
    lease_expires_at timestamptz,

    terminal_reason text,
    started_at timestamptz,
    finished_at timestamptz,

    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),

    FOREIGN KEY (credential_version_id, team_id)
        REFERENCES portal_team_proxy_credential_versions (id, team_id)
        ON DELETE RESTRICT,

    FOREIGN KEY (input_object_id, team_id)
        REFERENCES portal_object_references (id, team_id)
        ON DELETE RESTRICT,

    UNIQUE (id, team_id),

    CHECK (
        (state IN ('queued', 'running', 'cancelling') AND finished_at IS NULL)
        OR state IN ('completed', 'failed', 'cancelled')
    )
);

CREATE INDEX portal_jobs_fifo_idx
    ON portal_jobs (queue_sequence)
    WHERE state = 'queued';

CREATE INDEX portal_jobs_active_idx
    ON portal_jobs (state, queue_sequence)
    WHERE state IN ('running', 'cancelling');

CREATE INDEX portal_jobs_team_idx
    ON portal_jobs (team_id, queue_sequence DESC);

CREATE INDEX portal_jobs_lease_idx
    ON portal_jobs (lease_expires_at)
    WHERE state IN ('running', 'cancelling');

CREATE TRIGGER portal_jobs_sources_known
    BEFORE INSERT OR UPDATE OF sources ON portal_jobs
    FOR EACH ROW EXECUTE FUNCTION portal_check_sources_known();

CREATE TRIGGER portal_jobs_set_updated_at
    BEFORE UPDATE ON portal_jobs
    FOR EACH ROW EXECUTE FUNCTION portal_set_updated_at();

CREATE TABLE portal_job_items (
    id uuid PRIMARY KEY,
    job_id uuid NOT NULL REFERENCES portal_jobs(id) ON DELETE CASCADE,
    team_id uuid NOT NULL REFERENCES portal_teams(id) ON DELETE RESTRICT,

    ordinal integer NOT NULL CHECK (ordinal > 0),
    document text NOT NULL CHECK (length(trim(document)) > 0),

    -- NULL means excluded: the item was never assigned a site to run against.
    source text CONSTRAINT portal_job_items_source_known
        REFERENCES portal_sites (code),

    state text NOT NULL CHECK (
        state IN (
            'pending',
            'running',
            'published',
            'excluded',
            'failed',
            'cancelled'
        )
    ),

    reason text,

    -- Incremented when an item is leased to a worker.
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),

    lease_owner text,
    lease_fence bigint NOT NULL DEFAULT 0 CHECK (lease_fence >= 0),
    lease_expires_at timestamptz,

    result_object_id uuid,
    published_at timestamptz,
    finished_at timestamptz,

    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),

    UNIQUE (job_id, ordinal, source),

    FOREIGN KEY (job_id, team_id)
        REFERENCES portal_jobs (id, team_id)
        ON DELETE CASCADE,

    FOREIGN KEY (result_object_id, team_id)
        REFERENCES portal_object_references (id, team_id)
        ON DELETE RESTRICT,

    CONSTRAINT portal_job_items_excluded_has_no_source
        CHECK ((state = 'excluded') = (source IS NULL)),
    CHECK ((state = 'published') = (result_object_id IS NOT NULL))
);

CREATE INDEX portal_job_items_claim_idx
    ON portal_job_items (job_id, ordinal)
    WHERE state = 'pending';

CREATE INDEX portal_job_items_published_idx
    ON portal_job_items (document, job_id)
    WHERE state = 'published';

CREATE INDEX portal_job_items_lease_idx
    ON portal_job_items (lease_expires_at)
    WHERE state = 'running';

CREATE TRIGGER portal_job_items_set_updated_at
    BEFORE UPDATE ON portal_job_items
    FOR EACH ROW EXECUTE FUNCTION portal_set_updated_at();

CREATE TABLE portal_job_events (
    id uuid PRIMARY KEY,
    job_id uuid NOT NULL REFERENCES portal_jobs(id) ON DELETE CASCADE,
    sequence bigint GENERATED ALWAYS AS IDENTITY,
    event_type text NOT NULL CHECK (length(trim(event_type)) > 0),
    actor_id uuid REFERENCES portal_users(id) ON DELETE SET NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (job_id, sequence)
);

CREATE INDEX portal_job_events_job_idx
    ON portal_job_events (job_id, sequence);

CREATE TABLE portal_notification_outbox (
    id uuid PRIMARY KEY,
    event_id uuid NOT NULL REFERENCES portal_job_events(id) ON DELETE CASCADE,
    channel text NOT NULL CHECK (
        channel IN ('in_app', 'email', 'kapso_whatsapp')
    ),
    payload jsonb NOT NULL,
    state text NOT NULL DEFAULT 'pending' CHECK (
        state IN ('pending', 'sending', 'sent', 'failed')
    ),
    available_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (event_id, channel)
);

CREATE INDEX portal_notification_outbox_claim_idx
    ON portal_notification_outbox (available_at, id)
    WHERE state = 'pending';

CREATE TABLE portal_notification_deliveries (
    id uuid PRIMARY KEY,
    outbox_id uuid NOT NULL REFERENCES portal_notification_outbox(id) ON DELETE CASCADE,
    attempt integer NOT NULL CHECK (attempt > 0),
    provider_message_id text,
    outcome text NOT NULL CHECK (outcome IN ('sent', 'failed')),
    detail text,
    attempted_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (outbox_id, attempt)
);

-- A RUC-10 contains its owner's DNI, so searches need substring matching.
-- Btree indexes cannot serve a leading wildcard.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX portal_job_items_document_trgm_idx
    ON portal_job_items USING gin (document gin_trgm_ops)
    WHERE state = 'published';
