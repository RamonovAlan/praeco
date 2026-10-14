CREATE TABLE linkedin_oauth_sessions (
    state_hash TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    consumed_at TEXT
);

CREATE INDEX linkedin_oauth_sessions_expiry_idx
    ON linkedin_oauth_sessions (expires_at);
