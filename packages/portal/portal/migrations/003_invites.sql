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
