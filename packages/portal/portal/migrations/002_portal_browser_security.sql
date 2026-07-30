-- Browser sessions remain opaque: only a SHA-256 digest of the CSPRNG session ID
-- is retained. CSRF synchronizer values are server-side session state.
ALTER TABLE portal_sessions ADD COLUMN IF NOT EXISTS csrf_token text;

CREATE TABLE IF NOT EXISTS portal_login_csrf_tokens (
    token text PRIMARY KEY,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS portal_login_csrf_expiry_idx
    ON portal_login_csrf_tokens (expires_at);

CREATE TABLE IF NOT EXISTS portal_login_failures (
    email text NOT NULL,
    client_ip text NOT NULL,
    attempted_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS portal_login_failures_window_idx
    ON portal_login_failures (email, client_ip, attempted_at DESC);
