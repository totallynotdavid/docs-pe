--
-- PostgreSQL database dump
--



SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: public; Type: SCHEMA; Schema: -; Owner: -
--

-- *not* creating schema, since initdb creates it


--
-- Name: SCHEMA public; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON SCHEMA public IS '';


--
-- Name: pg_trgm; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;


--
-- Name: EXTENSION pg_trgm; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION pg_trgm IS 'text similarity measurement and index searching based on trigrams';


--
-- Name: portal_check_sources_known(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.portal_check_sources_known() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NOT (NEW.sources <@ (SELECT array_agg(code) FROM portal_sites)) THEN
        RAISE EXCEPTION 'unknown source in %', NEW.sources;
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: portal_lock_queue_control(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.portal_lock_queue_control() RETURNS smallint
    LANGUAGE plpgsql
    AS $$
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


--
-- Name: portal_reject_object_reference_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.portal_reject_object_reference_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    RAISE EXCEPTION 'portal object references are immutable';
END;
$$;


--
-- Name: portal_reject_proxy_credential_version_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.portal_reject_proxy_credential_version_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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


--
-- Name: portal_require_team_leader(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.portal_require_team_leader() RETURNS trigger
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


--
-- Name: portal_set_updated_at(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.portal_set_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: portal_installation_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.portal_installation_state (
    singleton boolean DEFAULT true NOT NULL,
    initial_team_id uuid,
    completed_by uuid,
    completed_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT portal_installation_state_check CHECK ((((initial_team_id IS NULL) AND (completed_by IS NULL) AND (completed_at IS NULL)) OR ((initial_team_id IS NOT NULL) AND (completed_by IS NOT NULL) AND (completed_at IS NOT NULL)))),
    CONSTRAINT portal_installation_state_singleton_check CHECK (singleton)
);


--
-- Name: portal_job_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.portal_job_events (
    id uuid NOT NULL,
    job_id uuid NOT NULL,
    sequence bigint NOT NULL,
    event_type text NOT NULL,
    actor_id uuid,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT portal_job_events_event_type_check CHECK ((length(TRIM(BOTH FROM event_type)) > 0))
);


--
-- Name: portal_job_events_sequence_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.portal_job_events ALTER COLUMN sequence ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.portal_job_events_sequence_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: portal_job_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.portal_job_items (
    id uuid NOT NULL,
    job_id uuid NOT NULL,
    team_id uuid NOT NULL,
    ordinal integer NOT NULL,
    document text NOT NULL,
    source text,
    state text NOT NULL,
    reason text,
    attempts integer DEFAULT 0 NOT NULL,
    lease_owner text,
    lease_fence bigint DEFAULT 0 NOT NULL,
    lease_expires_at timestamp with time zone,
    result_object_id uuid,
    published_at timestamp with time zone,
    finished_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT portal_job_items_attempts_check CHECK ((attempts >= 0)),
    CONSTRAINT portal_job_items_check CHECK (((state = 'published'::text) = (result_object_id IS NOT NULL))),
    CONSTRAINT portal_job_items_document_check CHECK ((length(TRIM(BOTH FROM document)) > 0)),
    CONSTRAINT portal_job_items_excluded_has_no_source CHECK (((state = 'excluded'::text) = (source IS NULL))),
    CONSTRAINT portal_job_items_lease_fence_check CHECK ((lease_fence >= 0)),
    CONSTRAINT portal_job_items_ordinal_check CHECK ((ordinal > 0)),
    CONSTRAINT portal_job_items_state_check CHECK ((state = ANY (ARRAY['pending'::text, 'running'::text, 'published'::text, 'excluded'::text, 'failed'::text, 'cancelled'::text])))
);


--
-- Name: portal_jobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.portal_jobs (
    id uuid NOT NULL,
    team_id uuid NOT NULL,
    submitted_by uuid NOT NULL,
    credential_version_id uuid NOT NULL,
    input_object_id uuid NOT NULL,
    filename text NOT NULL,
    sources text[] NOT NULL,
    state text NOT NULL,
    queue_sequence bigint NOT NULL,
    lease_owner text,
    lease_fence bigint DEFAULT 0 NOT NULL,
    lease_expires_at timestamp with time zone,
    terminal_reason text,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT portal_jobs_check CHECK ((((state = ANY (ARRAY['queued'::text, 'running'::text, 'cancelling'::text])) AND (finished_at IS NULL)) OR (state = ANY (ARRAY['completed'::text, 'failed'::text, 'cancelled'::text])))),
    CONSTRAINT portal_jobs_filename_check CHECK ((length(TRIM(BOTH FROM filename)) > 0)),
    CONSTRAINT portal_jobs_lease_fence_check CHECK ((lease_fence >= 0)),
    CONSTRAINT portal_jobs_sources_check CHECK ((cardinality(sources) > 0)),
    CONSTRAINT portal_jobs_state_check CHECK ((state = ANY (ARRAY['queued'::text, 'running'::text, 'cancelling'::text, 'completed'::text, 'failed'::text, 'cancelled'::text])))
);


--
-- Name: portal_jobs_queue_sequence_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.portal_jobs ALTER COLUMN queue_sequence ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.portal_jobs_queue_sequence_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: portal_login_csrf_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.portal_login_csrf_tokens (
    token text NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: portal_login_failures; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.portal_login_failures (
    email text NOT NULL,
    client_ip text NOT NULL,
    attempted_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: portal_notification_deliveries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.portal_notification_deliveries (
    id uuid NOT NULL,
    outbox_id uuid NOT NULL,
    attempt integer NOT NULL,
    provider_message_id text,
    outcome text NOT NULL,
    detail text,
    attempted_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT portal_notification_deliveries_attempt_check CHECK ((attempt > 0)),
    CONSTRAINT portal_notification_deliveries_outcome_check CHECK ((outcome = ANY (ARRAY['sent'::text, 'failed'::text])))
);


--
-- Name: portal_notification_outbox; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.portal_notification_outbox (
    id uuid NOT NULL,
    event_id uuid NOT NULL,
    channel text NOT NULL,
    payload jsonb NOT NULL,
    state text DEFAULT 'pending'::text NOT NULL,
    available_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT portal_notification_outbox_channel_check CHECK ((channel = ANY (ARRAY['in_app'::text, 'email'::text, 'kapso_whatsapp'::text]))),
    CONSTRAINT portal_notification_outbox_state_check CHECK ((state = ANY (ARRAY['pending'::text, 'sending'::text, 'sent'::text, 'failed'::text])))
);


--
-- Name: portal_object_references; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.portal_object_references (
    id uuid NOT NULL,
    team_id uuid NOT NULL,
    provider text NOT NULL,
    container text NOT NULL,
    object_key text NOT NULL,
    sha256 text NOT NULL,
    size_bytes bigint NOT NULL,
    content_type text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT portal_object_references_container_check CHECK ((length(TRIM(BOTH FROM container)) > 0)),
    CONSTRAINT portal_object_references_content_type_check CHECK ((length(TRIM(BOTH FROM content_type)) > 0)),
    CONSTRAINT portal_object_references_object_key_check CHECK ((length(TRIM(BOTH FROM object_key)) > 0)),
    CONSTRAINT portal_object_references_provider_check CHECK ((length(TRIM(BOTH FROM provider)) > 0)),
    CONSTRAINT portal_object_references_sha256_check CHECK ((sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT portal_object_references_size_bytes_check CHECK ((size_bytes >= 0))
);


--
-- Name: portal_proxy_credential_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.portal_proxy_credential_events (
    id uuid NOT NULL,
    credential_version_id uuid NOT NULL,
    from_lifecycle text NOT NULL,
    to_lifecycle text NOT NULL,
    detail text NOT NULL,
    actor_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT portal_proxy_credential_events_detail_check CHECK ((length(detail) <= 240)),
    CONSTRAINT portal_proxy_credential_events_from_lifecycle_check CHECK ((from_lifecycle = ANY (ARRAY['draft'::text, 'validating'::text, 'active'::text, 'failed'::text, 'retired'::text]))),
    CONSTRAINT portal_proxy_credential_events_to_lifecycle_check CHECK ((to_lifecycle = ANY (ARRAY['draft'::text, 'validating'::text, 'active'::text, 'failed'::text, 'retired'::text])))
);


--
-- Name: portal_queue_control; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.portal_queue_control (
    singleton boolean DEFAULT true NOT NULL,
    max_active_jobs smallint NOT NULL,
    CONSTRAINT portal_queue_control_max_active_jobs_check CHECK ((max_active_jobs = 5)),
    CONSTRAINT portal_queue_control_singleton_check CHECK (singleton)
);


--
-- Name: portal_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.portal_sessions (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    token_hash text NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    csrf_token text
);


--
-- Name: portal_sites; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.portal_sites (
    code text NOT NULL
);


--
-- Name: portal_team_memberships; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.portal_team_memberships (
    team_id uuid NOT NULL,
    user_id uuid NOT NULL,
    role text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT portal_team_memberships_role_check CHECK ((role = ANY (ARRAY['team_leader'::text, 'team_member'::text])))
);


--
-- Name: portal_team_proxy_credential_versions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.portal_team_proxy_credential_versions (
    id uuid NOT NULL,
    credential_id uuid NOT NULL,
    team_id uuid NOT NULL,
    version integer NOT NULL,
    provider text NOT NULL,
    config_ciphertext bytea CONSTRAINT portal_team_proxy_credential_version_config_ciphertext_not_null NOT NULL,
    key_id text NOT NULL,
    is_active boolean DEFAULT false NOT NULL,
    lifecycle text NOT NULL,
    validated_at timestamp with time zone,
    failure_detail text,
    created_by uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT portal_proxy_credential_active_consistent CHECK ((is_active = (lifecycle = 'active'::text))),
    CONSTRAINT portal_proxy_credential_lifecycle_valid CHECK ((lifecycle = ANY (ARRAY['draft'::text, 'validating'::text, 'active'::text, 'failed'::text, 'retired'::text]))),
    CONSTRAINT portal_proxy_provider_supported CHECK ((provider = ANY (ARRAY['geonode'::text, 'dataimpulse'::text]))),
    CONSTRAINT portal_team_proxy_credential_versions_config_ciphertext_check CHECK ((octet_length(config_ciphertext) > 0)),
    CONSTRAINT portal_team_proxy_credential_versions_key_id_check CHECK ((length(TRIM(BOTH FROM key_id)) > 0)),
    CONSTRAINT portal_team_proxy_credential_versions_version_check CHECK ((version > 0))
);


--
-- Name: portal_team_proxy_credentials; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.portal_team_proxy_credentials (
    id uuid NOT NULL,
    team_id uuid NOT NULL,
    label text NOT NULL,
    created_by uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    retired_at timestamp with time zone,
    CONSTRAINT portal_team_proxy_credentials_label_check CHECK ((length(TRIM(BOTH FROM label)) > 0))
);


--
-- Name: portal_teams; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.portal_teams (
    id uuid NOT NULL,
    slug text NOT NULL,
    name text NOT NULL,
    created_by uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT portal_teams_name_check CHECK ((length(TRIM(BOTH FROM name)) > 0)),
    CONSTRAINT portal_teams_slug_check CHECK ((slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'::text))
);


--
-- Name: portal_users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.portal_users (
    id uuid NOT NULL,
    email text NOT NULL,
    password_hash text NOT NULL,
    is_site_admin boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT portal_users_email_check CHECK ((email = lower(email)))
);


--
-- Name: portal_installation_state portal_installation_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_installation_state
    ADD CONSTRAINT portal_installation_state_pkey PRIMARY KEY (singleton);


--
-- Name: portal_job_events portal_job_events_job_id_sequence_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_job_events
    ADD CONSTRAINT portal_job_events_job_id_sequence_key UNIQUE (job_id, sequence);


--
-- Name: portal_job_events portal_job_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_job_events
    ADD CONSTRAINT portal_job_events_pkey PRIMARY KEY (id);


--
-- Name: portal_job_items portal_job_items_job_id_ordinal_source_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_job_items
    ADD CONSTRAINT portal_job_items_job_id_ordinal_source_key UNIQUE (job_id, ordinal, source);


--
-- Name: portal_job_items portal_job_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_job_items
    ADD CONSTRAINT portal_job_items_pkey PRIMARY KEY (id);


--
-- Name: portal_jobs portal_jobs_id_team_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_jobs
    ADD CONSTRAINT portal_jobs_id_team_id_key UNIQUE (id, team_id);


--
-- Name: portal_jobs portal_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_jobs
    ADD CONSTRAINT portal_jobs_pkey PRIMARY KEY (id);


--
-- Name: portal_jobs portal_jobs_queue_sequence_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_jobs
    ADD CONSTRAINT portal_jobs_queue_sequence_key UNIQUE (queue_sequence);


--
-- Name: portal_login_csrf_tokens portal_login_csrf_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_login_csrf_tokens
    ADD CONSTRAINT portal_login_csrf_tokens_pkey PRIMARY KEY (token);


--
-- Name: portal_notification_deliveries portal_notification_deliveries_outbox_id_attempt_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_notification_deliveries
    ADD CONSTRAINT portal_notification_deliveries_outbox_id_attempt_key UNIQUE (outbox_id, attempt);


--
-- Name: portal_notification_deliveries portal_notification_deliveries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_notification_deliveries
    ADD CONSTRAINT portal_notification_deliveries_pkey PRIMARY KEY (id);


--
-- Name: portal_notification_outbox portal_notification_outbox_event_id_channel_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_notification_outbox
    ADD CONSTRAINT portal_notification_outbox_event_id_channel_key UNIQUE (event_id, channel);


--
-- Name: portal_notification_outbox portal_notification_outbox_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_notification_outbox
    ADD CONSTRAINT portal_notification_outbox_pkey PRIMARY KEY (id);


--
-- Name: portal_object_references portal_object_references_id_team_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_object_references
    ADD CONSTRAINT portal_object_references_id_team_id_key UNIQUE (id, team_id);


--
-- Name: portal_object_references portal_object_references_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_object_references
    ADD CONSTRAINT portal_object_references_pkey PRIMARY KEY (id);


--
-- Name: portal_object_references portal_object_references_team_id_provider_container_object__key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_object_references
    ADD CONSTRAINT portal_object_references_team_id_provider_container_object__key UNIQUE (team_id, provider, container, object_key, sha256);


--
-- Name: portal_proxy_credential_events portal_proxy_credential_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_proxy_credential_events
    ADD CONSTRAINT portal_proxy_credential_events_pkey PRIMARY KEY (id);


--
-- Name: portal_queue_control portal_queue_control_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_queue_control
    ADD CONSTRAINT portal_queue_control_pkey PRIMARY KEY (singleton);


--
-- Name: portal_sessions portal_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_sessions
    ADD CONSTRAINT portal_sessions_pkey PRIMARY KEY (id);


--
-- Name: portal_sessions portal_sessions_token_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_sessions
    ADD CONSTRAINT portal_sessions_token_hash_key UNIQUE (token_hash);


--
-- Name: portal_sites portal_sites_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_sites
    ADD CONSTRAINT portal_sites_pkey PRIMARY KEY (code);


--
-- Name: portal_team_memberships portal_team_memberships_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_team_memberships
    ADD CONSTRAINT portal_team_memberships_pkey PRIMARY KEY (team_id, user_id);


--
-- Name: portal_team_proxy_credential_versions portal_team_proxy_credential_versions_credential_id_version_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_team_proxy_credential_versions
    ADD CONSTRAINT portal_team_proxy_credential_versions_credential_id_version_key UNIQUE (credential_id, version);


--
-- Name: portal_team_proxy_credential_versions portal_team_proxy_credential_versions_id_team_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_team_proxy_credential_versions
    ADD CONSTRAINT portal_team_proxy_credential_versions_id_team_id_key UNIQUE (id, team_id);


--
-- Name: portal_team_proxy_credential_versions portal_team_proxy_credential_versions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_team_proxy_credential_versions
    ADD CONSTRAINT portal_team_proxy_credential_versions_pkey PRIMARY KEY (id);


--
-- Name: portal_team_proxy_credentials portal_team_proxy_credentials_id_team_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_team_proxy_credentials
    ADD CONSTRAINT portal_team_proxy_credentials_id_team_id_key UNIQUE (id, team_id);


--
-- Name: portal_team_proxy_credentials portal_team_proxy_credentials_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_team_proxy_credentials
    ADD CONSTRAINT portal_team_proxy_credentials_pkey PRIMARY KEY (id);


--
-- Name: portal_team_proxy_credentials portal_team_proxy_credentials_team_id_label_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_team_proxy_credentials
    ADD CONSTRAINT portal_team_proxy_credentials_team_id_label_key UNIQUE (team_id, label);


--
-- Name: portal_teams portal_teams_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_teams
    ADD CONSTRAINT portal_teams_pkey PRIMARY KEY (id);


--
-- Name: portal_teams portal_teams_slug_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_teams
    ADD CONSTRAINT portal_teams_slug_key UNIQUE (slug);


--
-- Name: portal_users portal_users_email_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_users
    ADD CONSTRAINT portal_users_email_key UNIQUE (email);


--
-- Name: portal_users portal_users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_users
    ADD CONSTRAINT portal_users_pkey PRIMARY KEY (id);


--
-- Name: portal_active_credential_lifecycle_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX portal_active_credential_lifecycle_idx ON public.portal_team_proxy_credential_versions USING btree (credential_id) WHERE (lifecycle = 'active'::text);


--
-- Name: portal_job_events_job_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX portal_job_events_job_idx ON public.portal_job_events USING btree (job_id, sequence);


--
-- Name: portal_job_items_claim_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX portal_job_items_claim_idx ON public.portal_job_items USING btree (job_id, ordinal) WHERE (state = 'pending'::text);


--
-- Name: portal_job_items_document_trgm_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX portal_job_items_document_trgm_idx ON public.portal_job_items USING gin (document public.gin_trgm_ops) WHERE (state = 'published'::text);


--
-- Name: portal_job_items_lease_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX portal_job_items_lease_idx ON public.portal_job_items USING btree (lease_expires_at) WHERE (state = 'running'::text);


--
-- Name: portal_job_items_published_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX portal_job_items_published_idx ON public.portal_job_items USING btree (document, job_id) WHERE (state = 'published'::text);


--
-- Name: portal_jobs_active_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX portal_jobs_active_idx ON public.portal_jobs USING btree (state, queue_sequence) WHERE (state = ANY (ARRAY['running'::text, 'cancelling'::text]));


--
-- Name: portal_jobs_fifo_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX portal_jobs_fifo_idx ON public.portal_jobs USING btree (queue_sequence) WHERE (state = 'queued'::text);


--
-- Name: portal_jobs_lease_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX portal_jobs_lease_idx ON public.portal_jobs USING btree (lease_expires_at) WHERE (state = ANY (ARRAY['running'::text, 'cancelling'::text]));


--
-- Name: portal_jobs_team_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX portal_jobs_team_idx ON public.portal_jobs USING btree (team_id, queue_sequence DESC);


--
-- Name: portal_login_csrf_expiry_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX portal_login_csrf_expiry_idx ON public.portal_login_csrf_tokens USING btree (expires_at);


--
-- Name: portal_login_failures_window_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX portal_login_failures_window_idx ON public.portal_login_failures USING btree (email, client_ip, attempted_at DESC);


--
-- Name: portal_memberships_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX portal_memberships_user_idx ON public.portal_team_memberships USING btree (user_id, team_id);


--
-- Name: portal_notification_outbox_claim_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX portal_notification_outbox_claim_idx ON public.portal_notification_outbox USING btree (available_at, id) WHERE (state = 'pending'::text);


--
-- Name: portal_proxy_credential_events_version_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX portal_proxy_credential_events_version_idx ON public.portal_proxy_credential_events USING btree (credential_version_id, created_at);


--
-- Name: portal_sessions_expiry_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX portal_sessions_expiry_idx ON public.portal_sessions USING btree (expires_at);


--
-- Name: portal_installation_state portal_installation_state_set_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER portal_installation_state_set_updated_at BEFORE UPDATE ON public.portal_installation_state FOR EACH ROW EXECUTE FUNCTION public.portal_set_updated_at();


--
-- Name: portal_job_items portal_job_items_set_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER portal_job_items_set_updated_at BEFORE UPDATE ON public.portal_job_items FOR EACH ROW EXECUTE FUNCTION public.portal_set_updated_at();


--
-- Name: portal_jobs portal_jobs_set_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER portal_jobs_set_updated_at BEFORE UPDATE ON public.portal_jobs FOR EACH ROW EXECUTE FUNCTION public.portal_set_updated_at();


--
-- Name: portal_jobs portal_jobs_sources_known; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER portal_jobs_sources_known BEFORE INSERT OR UPDATE OF sources ON public.portal_jobs FOR EACH ROW EXECUTE FUNCTION public.portal_check_sources_known();


--
-- Name: portal_object_references portal_object_references_immutable; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER portal_object_references_immutable BEFORE DELETE OR UPDATE ON public.portal_object_references FOR EACH ROW EXECUTE FUNCTION public.portal_reject_object_reference_mutation();


--
-- Name: portal_team_proxy_credential_versions portal_proxy_credential_versions_immutable; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER portal_proxy_credential_versions_immutable BEFORE UPDATE ON public.portal_team_proxy_credential_versions FOR EACH ROW EXECUTE FUNCTION public.portal_reject_proxy_credential_version_mutation();


--
-- Name: portal_team_memberships portal_team_must_have_leader; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER portal_team_must_have_leader AFTER INSERT OR DELETE OR UPDATE OF role ON public.portal_team_memberships DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.portal_require_team_leader();


--
-- Name: portal_installation_state portal_installation_state_completed_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_installation_state
    ADD CONSTRAINT portal_installation_state_completed_by_fkey FOREIGN KEY (completed_by) REFERENCES public.portal_users(id) ON DELETE RESTRICT;


--
-- Name: portal_installation_state portal_installation_state_initial_team_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_installation_state
    ADD CONSTRAINT portal_installation_state_initial_team_id_fkey FOREIGN KEY (initial_team_id) REFERENCES public.portal_teams(id) ON DELETE RESTRICT;


--
-- Name: portal_job_events portal_job_events_actor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_job_events
    ADD CONSTRAINT portal_job_events_actor_id_fkey FOREIGN KEY (actor_id) REFERENCES public.portal_users(id) ON DELETE SET NULL;


--
-- Name: portal_job_events portal_job_events_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_job_events
    ADD CONSTRAINT portal_job_events_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.portal_jobs(id) ON DELETE CASCADE;


--
-- Name: portal_job_items portal_job_items_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_job_items
    ADD CONSTRAINT portal_job_items_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.portal_jobs(id) ON DELETE CASCADE;


--
-- Name: portal_job_items portal_job_items_job_id_team_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_job_items
    ADD CONSTRAINT portal_job_items_job_id_team_id_fkey FOREIGN KEY (job_id, team_id) REFERENCES public.portal_jobs(id, team_id) ON DELETE CASCADE;


--
-- Name: portal_job_items portal_job_items_result_object_id_team_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_job_items
    ADD CONSTRAINT portal_job_items_result_object_id_team_id_fkey FOREIGN KEY (result_object_id, team_id) REFERENCES public.portal_object_references(id, team_id) ON DELETE RESTRICT;


--
-- Name: portal_job_items portal_job_items_source_known; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_job_items
    ADD CONSTRAINT portal_job_items_source_known FOREIGN KEY (source) REFERENCES public.portal_sites(code);


--
-- Name: portal_job_items portal_job_items_team_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_job_items
    ADD CONSTRAINT portal_job_items_team_id_fkey FOREIGN KEY (team_id) REFERENCES public.portal_teams(id) ON DELETE RESTRICT;


--
-- Name: portal_jobs portal_jobs_credential_version_id_team_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_jobs
    ADD CONSTRAINT portal_jobs_credential_version_id_team_id_fkey FOREIGN KEY (credential_version_id, team_id) REFERENCES public.portal_team_proxy_credential_versions(id, team_id) ON DELETE RESTRICT;


--
-- Name: portal_jobs portal_jobs_input_object_id_team_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_jobs
    ADD CONSTRAINT portal_jobs_input_object_id_team_id_fkey FOREIGN KEY (input_object_id, team_id) REFERENCES public.portal_object_references(id, team_id) ON DELETE RESTRICT;


--
-- Name: portal_jobs portal_jobs_submitted_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_jobs
    ADD CONSTRAINT portal_jobs_submitted_by_fkey FOREIGN KEY (submitted_by) REFERENCES public.portal_users(id);


--
-- Name: portal_jobs portal_jobs_team_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_jobs
    ADD CONSTRAINT portal_jobs_team_id_fkey FOREIGN KEY (team_id) REFERENCES public.portal_teams(id) ON DELETE RESTRICT;


--
-- Name: portal_notification_deliveries portal_notification_deliveries_outbox_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_notification_deliveries
    ADD CONSTRAINT portal_notification_deliveries_outbox_id_fkey FOREIGN KEY (outbox_id) REFERENCES public.portal_notification_outbox(id) ON DELETE CASCADE;


--
-- Name: portal_notification_outbox portal_notification_outbox_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_notification_outbox
    ADD CONSTRAINT portal_notification_outbox_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.portal_job_events(id) ON DELETE CASCADE;


--
-- Name: portal_object_references portal_object_references_team_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_object_references
    ADD CONSTRAINT portal_object_references_team_id_fkey FOREIGN KEY (team_id) REFERENCES public.portal_teams(id) ON DELETE RESTRICT;


--
-- Name: portal_proxy_credential_events portal_proxy_credential_events_actor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_proxy_credential_events
    ADD CONSTRAINT portal_proxy_credential_events_actor_id_fkey FOREIGN KEY (actor_id) REFERENCES public.portal_users(id) ON DELETE SET NULL;


--
-- Name: portal_proxy_credential_events portal_proxy_credential_events_credential_version_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_proxy_credential_events
    ADD CONSTRAINT portal_proxy_credential_events_credential_version_id_fkey FOREIGN KEY (credential_version_id) REFERENCES public.portal_team_proxy_credential_versions(id) ON DELETE RESTRICT;


--
-- Name: portal_sessions portal_sessions_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_sessions
    ADD CONSTRAINT portal_sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.portal_users(id) ON DELETE CASCADE;


--
-- Name: portal_team_memberships portal_team_memberships_team_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_team_memberships
    ADD CONSTRAINT portal_team_memberships_team_id_fkey FOREIGN KEY (team_id) REFERENCES public.portal_teams(id) ON DELETE CASCADE;


--
-- Name: portal_team_memberships portal_team_memberships_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_team_memberships
    ADD CONSTRAINT portal_team_memberships_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.portal_users(id) ON DELETE CASCADE;


--
-- Name: portal_team_proxy_credential_versions portal_team_proxy_credential_version_credential_id_team_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_team_proxy_credential_versions
    ADD CONSTRAINT portal_team_proxy_credential_version_credential_id_team_id_fkey FOREIGN KEY (credential_id, team_id) REFERENCES public.portal_team_proxy_credentials(id, team_id) ON DELETE CASCADE;


--
-- Name: portal_team_proxy_credential_versions portal_team_proxy_credential_versions_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_team_proxy_credential_versions
    ADD CONSTRAINT portal_team_proxy_credential_versions_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.portal_users(id);


--
-- Name: portal_team_proxy_credentials portal_team_proxy_credentials_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_team_proxy_credentials
    ADD CONSTRAINT portal_team_proxy_credentials_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.portal_users(id);


--
-- Name: portal_team_proxy_credentials portal_team_proxy_credentials_team_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_team_proxy_credentials
    ADD CONSTRAINT portal_team_proxy_credentials_team_id_fkey FOREIGN KEY (team_id) REFERENCES public.portal_teams(id) ON DELETE CASCADE;


--
-- Name: portal_teams portal_teams_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_teams
    ADD CONSTRAINT portal_teams_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.portal_users(id);


--
-- PostgreSQL database dump complete
--


