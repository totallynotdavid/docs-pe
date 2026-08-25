-- The whole portal schema. One file, because the installation is created from
-- scratch: a migration chain that replays history nobody has is a liability,
-- not a record. The second file added here will be the first one written
-- against a database that holds real data.

-- RUC-10 searches match an embedded DNI, which requires leading-wildcard
-- substring search, used by portal_entries_document_trgm_idx below.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Sites live in one lookup table so the database and
-- fetch.sites.registry.STABLE_SITES have one list to keep synchronized.
CREATE TABLE portal_sites (
    code text PRIMARY KEY
);

INSERT INTO portal_sites (code)
VALUES ('osiptel'), ('sunat'), ('sunat_reps');

-- TOTP secrets are enveloped exactly like proxy credentials: the payload is
-- AES-GCM under a per-secret data key, and only the wrapped data key is stored.
CREATE TABLE portal_users (
    id uuid PRIMARY KEY,
    email text NOT NULL UNIQUE CHECK (email = lower(email)),
    password_hash text NOT NULL,
    is_site_admin boolean NOT NULL DEFAULT false,

    -- Set while an invited site admin has registered but not yet enrolled a
    -- second factor; portal_require_admin_second_factor below refuses to let
    -- is_site_admin itself go true until one exists.
    pending_site_admin boolean NOT NULL DEFAULT false,

    -- A deactivated account keeps every row it ever created (teams, jobs,
    -- credentials) intact; only login and role guards look at this.
    is_active boolean NOT NULL DEFAULT true,
    deactivated_at timestamptz,
    deactivated_by uuid REFERENCES portal_users(id),

    mfa_secret_ciphertext bytea,
    mfa_secret_wrapped_data_key bytea,
    mfa_secret_master_key_version text,
    mfa_enabled boolean NOT NULL DEFAULT false,

    created_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT portal_mfa_enabled_requires_secret CHECK (
        NOT mfa_enabled
        OR (
            mfa_secret_ciphertext IS NOT NULL
            AND mfa_secret_wrapped_data_key IS NOT NULL
            AND mfa_secret_master_key_version IS NOT NULL
        )
    ),

    CONSTRAINT portal_pending_site_admin_not_admin
        CHECK (NOT (is_site_admin AND pending_site_admin)),

    CONSTRAINT portal_user_active_consistent CHECK (is_active = (deactivated_at IS NULL))
);

-- Single-use, hashed at rest. Codes carry 128 bits of randomness, so SHA-256 is
-- enough: unlike a password there is no guessable input to grind against.
CREATE TABLE portal_mfa_recovery_codes (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES portal_users(id) ON DELETE CASCADE,
    code_hash text NOT NULL CHECK (code_hash ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    used_at timestamptz,
    UNIQUE (user_id, code_hash)
);

CREATE INDEX portal_mfa_recovery_codes_unused_idx
    ON portal_mfa_recovery_codes (user_id)
    WHERE used_at IS NULL;

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

-- A site administrator's second factor can be TOTP (mfa_enabled) or any row in
-- portal_webauthn_credentials; a single CHECK on portal_users can't see across
-- tables, so the invariant is a constraint trigger instead, mirroring
-- portal_require_site_admin below.
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

-- Sessions, rate-limit counters, and single-use tokens. Never a system of
-- record: dropping every row here logs everyone out and resets every counter,
-- and costs nothing else. It is a Postgres table rather than Redis because at
-- this installation's size a second stateful service buys no measurable
-- headroom while adding a failure mode that stops logins.
CREATE TABLE portal_ephemeral (
    key text PRIMARY KEY,
    value text NOT NULL,
    expires_at timestamptz NOT NULL
);

CREATE INDEX portal_ephemeral_expiry_idx
    ON portal_ephemeral (expires_at);

CREATE TABLE portal_teams (
    id uuid PRIMARY KEY,
    slug text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
    name text NOT NULL CHECK (length(trim(name)) > 0),
    created_by uuid NOT NULL REFERENCES portal_users(id),
    created_at timestamptz NOT NULL DEFAULT now(),

    -- Entitlement, not a role: a team searching beyond what it scanned itself
    -- is a paid capability, orthogonal to who administers the installation.
    -- See portal.application.access.site_admin_or_global_search.
    has_global_search boolean NOT NULL DEFAULT false
);

CREATE TABLE portal_team_memberships (
    team_id uuid NOT NULL REFERENCES portal_teams(id) ON DELETE CASCADE,
    user_id uuid NOT NULL REFERENCES portal_users(id) ON DELETE CASCADE,
    role text NOT NULL CHECK (role IN ('team_leader', 'team_member')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (team_id, user_id)
);

CREATE INDEX portal_memberships_user_idx
    ON portal_team_memberships (user_id, team_id);

-- Deferred validation allows the team and its first leader to be created in one
-- transaction. Locking the team serializes concurrent leader removals.
CREATE OR REPLACE FUNCTION portal_require_team_leader()
RETURNS trigger
LANGUAGE plpgsql
AS $$
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

-- Self-service team invites: a leader or site admin invites by email and no
-- account needs to exist yet. One live invite per (team_id, email); a
-- re-invite replaces it in place rather than accumulating history, the same
-- way portal_ephemeral treats a still-pending key on INSERT ... ON CONFLICT.
-- History, when it matters, is in portal_audit_log (INVITE_SENT/INVITE_ACCEPTED).
CREATE TABLE portal_team_invites (
    id uuid PRIMARY KEY,
    team_id uuid NOT NULL REFERENCES portal_teams(id) ON DELETE CASCADE,
    email text NOT NULL CHECK (email = lower(email)),
    role text NOT NULL CHECK (role IN ('team_leader', 'team_member')),
    token_hash text NOT NULL UNIQUE CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    invited_by uuid NOT NULL REFERENCES portal_users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    accepted_at timestamptz,
    UNIQUE (team_id, email)
);

CREATE INDEX portal_team_invites_pending_idx
    ON portal_team_invites (team_id)
    WHERE accepted_at IS NULL;

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
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'portal object references are immutable';
END;
$$;

CREATE TRIGGER portal_object_references_immutable
    BEFORE UPDATE OR DELETE ON portal_object_references
    FOR EACH ROW
    EXECUTE FUNCTION portal_reject_object_reference_mutation();

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

    -- The envelope: ciphertext under a data key that exists only in wrapped
    -- form here, and the master key version that can unwrap it.
    config_ciphertext bytea NOT NULL
        CHECK (octet_length(config_ciphertext) > 0),
    wrapped_data_key bytea NOT NULL
        CHECK (octet_length(wrapped_data_key) > 0),
    master_key_version text NOT NULL CHECK (length(trim(master_key_version)) > 0),

    -- Kept as a boolean for direct filtering and synchronized with lifecycle.
    is_active boolean NOT NULL DEFAULT false,
    lifecycle text NOT NULL CONSTRAINT portal_proxy_credential_lifecycle_valid
        CHECK (
            lifecycle IN ('draft', 'validating', 'active', 'failed', 'retired')
        ),
    CONSTRAINT portal_proxy_credential_active_consistent
        CHECK (is_active = (lifecycle = 'active')),

    validated_at timestamptz,
    failure_detail text,

    created_by uuid NOT NULL REFERENCES portal_users(id),
    created_at timestamptz NOT NULL DEFAULT now(),

    FOREIGN KEY (credential_id, team_id)
        REFERENCES portal_team_proxy_credentials (id, team_id)
        ON DELETE CASCADE,

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
    ON portal_proxy_credential_events (
        credential_version_id,
        created_at
    );

-- Rotation re-wraps the data key and rewrites master_key_version, so those two
-- are the only columns a stored credential is allowed to change.
CREATE OR REPLACE FUNCTION portal_reject_proxy_credential_version_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.credential_id IS DISTINCT FROM OLD.credential_id
       OR NEW.team_id IS DISTINCT FROM OLD.team_id
       OR NEW.version IS DISTINCT FROM OLD.version
       OR NEW.provider IS DISTINCT FROM OLD.provider
       OR NEW.config_ciphertext IS DISTINCT FROM OLD.config_ciphertext
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

-- Keeps updated_at tied to the last database write.
CREATE OR REPLACE FUNCTION portal_set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
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

INSERT INTO portal_installation_state (singleton)
VALUES (true);

CREATE TRIGGER portal_installation_state_set_updated_at
    BEFORE UPDATE ON portal_installation_state
    FOR EACH ROW
    EXECUTE FUNCTION portal_set_updated_at();

-- Mirrors portal_require_team_leader: deactivating or demoting the last site
-- administrator would leave nobody able to administer the installation.
-- portal_installation_state's singleton row is locked as the mutex, the same
-- role the team row plays for portal_require_team_leader, since there is no
-- natural parent row for a site-wide invariant over portal_users itself.
CREATE OR REPLACE FUNCTION portal_require_site_admin()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM 1 FROM portal_installation_state WHERE singleton = true FOR UPDATE;

    IF NOT EXISTS (
        SELECT 1 FROM portal_users WHERE is_site_admin AND is_active
    ) THEN
        RAISE EXCEPTION 'the installation must retain at least one active site administrator'
            USING ERRCODE = '23514';
    END IF;

    RETURN NEW;
END;
$$;

CREATE CONSTRAINT TRIGGER portal_installation_must_have_admin
    AFTER UPDATE OF is_site_admin, is_active
    ON portal_users
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    EXECUTE FUNCTION portal_require_site_admin();

CREATE TABLE portal_queue_control (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),

    -- Global concurrent job limit.
    max_active_jobs smallint NOT NULL CHECK (max_active_jobs = 5)
);

INSERT INTO portal_queue_control (singleton, max_active_jobs)
VALUES (true, 5);

-- SQL entry point for the queue mutex. Repositories issue the same
-- SELECT ... FOR UPDATE directly.
CREATE OR REPLACE FUNCTION portal_lock_queue_control()
RETURNS smallint
LANGUAGE plpgsql
AS $$
DECLARE
    maximum smallint;
BEGIN
    SELECT max_active_jobs
      INTO maximum
      FROM portal_queue_control
     WHERE singleton = true
     FOR UPDATE;

    RETURN maximum;
END;
$$;

-- PostgreSQL foreign keys cannot validate individual array elements.
CREATE OR REPLACE FUNCTION portal_check_sources_known()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT (
        NEW.sources <@ (
            SELECT array_agg(code)
              FROM portal_sites
        )
    ) THEN
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
    FOR EACH ROW
    EXECUTE FUNCTION portal_check_sources_known();

CREATE TRIGGER portal_jobs_set_updated_at
    BEFORE UPDATE ON portal_jobs
    FOR EACH ROW
    EXECUTE FUNCTION portal_set_updated_at();

-- The deduplicated, current-knowledge store: one row per (document, source),
-- shared across every team that has ever scanned it. portal_job_items (below)
-- is the append-only per-team ledger of who confirmed what, when; this table
-- is what search and result views actually read.
--
-- columns/rows mirror the wire shape portal.worker.agent already sends
-- (WorkerAgent._execute: {"columns": [...], "rows": [[...], ...]}), which
-- itself mirrors fetch.domain.types.Site.columns and Result.rows. Storing
-- that shape verbatim, rather than typed per-source columns, keeps this
-- table ignorant of what a source's fields are named -- the same boundary
-- portal.worker.routes already draws ("the payload is a fetch result whose
-- shape belongs to the site"). A new fetch site never requires a migration
-- here.
CREATE TABLE portal_entries (
    id uuid PRIMARY KEY,
    document text NOT NULL CHECK (length(trim(document)) > 0),
    source text NOT NULL CONSTRAINT portal_entries_source_known
        REFERENCES portal_sites (code),

    status text NOT NULL CHECK (status IN ('ok', 'not_found', 'failed')),
    columns text[] NOT NULL,
    rows jsonb NOT NULL,
    error_code text,

    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_confirmed_at timestamptz NOT NULL DEFAULT now(),

    -- Provenance only: which job most recently confirmed this value. Never a
    -- visibility check -- a team's access to an entry runs through its own
    -- portal_job_items rows, not through this column.
    last_job_id uuid NOT NULL REFERENCES portal_jobs(id) ON DELETE RESTRICT,

    UNIQUE (document, source)
);

CREATE INDEX portal_entries_document_trgm_idx
    ON portal_entries
    USING gin (document gin_trgm_ops);

CREATE TABLE portal_job_items (
    id uuid PRIMARY KEY,
    job_id uuid NOT NULL REFERENCES portal_jobs(id) ON DELETE CASCADE,
    team_id uuid NOT NULL REFERENCES portal_teams(id) ON DELETE RESTRICT,

    ordinal integer NOT NULL CHECK (ordinal > 0),
    document text NOT NULL CHECK (length(trim(document)) > 0),

    -- NULL means the item was excluded before being assigned to a site.
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

    -- Incremented whenever the item is leased to a worker.
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),

    lease_owner text,
    lease_fence bigint NOT NULL DEFAULT 0 CHECK (lease_fence >= 0),
    lease_expires_at timestamptz,

    -- The queryable content this item confirmed. Set together with
    -- result_object_id on a fresh fetch; set alone, pointing at a
    -- pre-existing row, when the submission review step reused a fresh
    -- entry instead of re-fetching (see portal.domain.planning).
    entry_id uuid REFERENCES portal_entries(id) ON DELETE RESTRICT,

    -- The raw captured payload for this specific run, kept only for audit
    -- and replay. Optional even when published: a reused item sets entry_id
    -- without ever making a fresh capture, so there is nothing to archive.
    result_object_id uuid,
    published_at timestamptz,
    finished_at timestamptz,

    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),

    FOREIGN KEY (job_id, team_id)
        REFERENCES portal_jobs (id, team_id)
        ON DELETE CASCADE,

    FOREIGN KEY (result_object_id, team_id)
        REFERENCES portal_object_references (id, team_id)
        ON DELETE RESTRICT,

    CONSTRAINT portal_job_items_excluded_has_no_source
        CHECK ((state = 'excluded') = (source IS NULL)),

    CHECK ((state = 'published') = (entry_id IS NOT NULL))
);

CREATE UNIQUE INDEX portal_job_items_source_unique_idx
    ON portal_job_items (job_id, ordinal, source)
    WHERE source IS NOT NULL;

CREATE UNIQUE INDEX portal_job_items_excluded_unique_idx
    ON portal_job_items (job_id, ordinal)
    WHERE source IS NULL;

CREATE INDEX portal_job_items_claim_idx
    ON portal_job_items (job_id, ordinal)
    WHERE state = 'pending';

-- Search join: "everything this team has published", newest first.
CREATE INDEX portal_job_items_team_entry_idx
    ON portal_job_items (team_id, entry_id)
    WHERE state = 'published';

-- Submission-review reuse check: "has this team already confirmed
-- (document, source) recently", scoped to that team alone. A cross-team
-- match must never surface here -- see portal.repository.entries.
CREATE INDEX portal_job_items_team_document_idx
    ON portal_job_items (team_id, document, source)
    WHERE state = 'published';

CREATE INDEX portal_job_items_lease_idx
    ON portal_job_items (lease_expires_at)
    WHERE state = 'running';

CREATE TRIGGER portal_job_items_set_updated_at
    BEFORE UPDATE ON portal_job_items
    FOR EACH ROW
    EXECUTE FUNCTION portal_set_updated_at();

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

-- Append-only, like portal_job_events: nothing ever mutates a past search, so
-- there is no update/delete trigger to write. This is what "usage/traffic"
-- visibility for team leads and admins reads from.
CREATE TABLE portal_search_log (
    id uuid PRIMARY KEY,
    team_id uuid NOT NULL REFERENCES portal_teams(id) ON DELETE CASCADE,
    actor_id uuid NOT NULL REFERENCES portal_users(id),
    query text NOT NULL CHECK (length(trim(query)) > 0),
    result_count integer NOT NULL CHECK (result_count >= 0),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX portal_search_log_team_idx
    ON portal_search_log (team_id, created_at DESC);

CREATE TABLE portal_notification_outbox (
    id uuid PRIMARY KEY,
    event_id uuid NOT NULL
        REFERENCES portal_job_events(id)
        ON DELETE CASCADE,
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
    outbox_id uuid NOT NULL
        REFERENCES portal_notification_outbox(id)
        ON DELETE CASCADE,
    attempt integer NOT NULL CHECK (attempt > 0),
    provider_message_id text,
    outcome text NOT NULL CHECK (outcome IN ('sent', 'failed')),
    detail text,
    attempted_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (outbox_id, attempt)
);

-- Tailscale membership proves a request came from the tailnet, not which worker
-- sent it. This is the identity claim and publish authenticate against, and
-- revoking a row takes effect on the next request rather than waiting for ACL
-- propagation.
CREATE TABLE portal_workers (
    id uuid PRIMARY KEY,
    worker_id text NOT NULL UNIQUE CHECK (worker_id ~ '^[A-Za-z0-9_-]{1,64}$'),
    credential_hash text NOT NULL CHECK (credential_hash ~ '^[0-9a-f]{64}$'),
    tailscale_hostname text NOT NULL CHECK (length(trim(tailscale_hostname)) > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,

    -- Current-state only, overwritten on every heartbeat. No history table:
    -- the ask is "is the fleet healthy right now," not a metrics time series.
    last_seen_at timestamptz,
    cpu_percent real CHECK (cpu_percent IS NULL OR cpu_percent >= 0),
    memory_mb real CHECK (memory_mb IS NULL OR memory_mb >= 0),
    current_job_id uuid REFERENCES portal_jobs(id) ON DELETE SET NULL
);

-- Fleet-wide circuit breaker state, keyed by (site, provider). Mirrors
-- fetch.pipeline.breaker's threshold and cooldown formula (see
-- portal/repository/breakers.py, which imports the constants rather than
-- redefining them) but lives in Postgres instead of one agent process's
-- memory: with N worker nodes each running their own in-process breaker,
-- a systemic failure needed ~10*N wasted attempts fleet-wide before every
-- lane actually stopped. This table is the shared backstop /claim filters
-- against, so ten consecutive failures anywhere park every node's lanes for
-- that pair, matching the single-process invariant docs/architecture.md
-- describes.
CREATE TABLE portal_circuit_breakers (
    source text NOT NULL REFERENCES portal_sites (code),
    provider text NOT NULL CONSTRAINT portal_circuit_breakers_provider_supported
        CHECK (provider IN ('geonode', 'dataimpulse')),

    consecutive_failures integer NOT NULL DEFAULT 0
        CHECK (consecutive_failures >= 0),
    level integer NOT NULL DEFAULT 0 CHECK (level >= 0),
    open_until timestamptz,

    updated_at timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (source, provider)
);

CREATE TRIGGER portal_circuit_breakers_set_updated_at
    BEFORE UPDATE ON portal_circuit_breakers
    FOR EACH ROW
    EXECUTE FUNCTION portal_set_updated_at();

CREATE TABLE portal_audit_log (
    id uuid PRIMARY KEY,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    actor_id uuid,
    action text NOT NULL CHECK (length(trim(action)) > 0),
    target_type text,
    target_id uuid,
    ip inet,
    cf_ray_id text,
    metadata jsonb NOT NULL DEFAULT '{}'
);

CREATE INDEX portal_audit_log_actor_idx
    ON portal_audit_log (actor_id, occurred_at DESC);

CREATE INDEX portal_audit_log_action_idx
    ON portal_audit_log (action, occurred_at DESC);

-- Insert-only at the grant level. An application role that can rewrite its own
-- audit trail is not an audit trail. The REVOKE is skipped when portal_app has
-- not been created, which is the case for local clusters and the test suite.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'portal_app') THEN
        REVOKE UPDATE, DELETE ON portal_audit_log FROM portal_app;
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION portal_reject_audit_log_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'portal audit log entries are immutable';
END;
$$;

CREATE TRIGGER portal_audit_log_immutable
    BEFORE UPDATE OR DELETE ON portal_audit_log
    FOR EACH ROW
    EXECUTE FUNCTION portal_reject_audit_log_mutation();
