CREATE TABLE drafts (
    id TEXT PRIMARY KEY,
    owner_id INTEGER NOT NULL CHECK (owner_id > 0),
    source_kind TEXT NOT NULL CHECK (source_kind IN ('text', 'voice')),
    source_text TEXT NOT NULL,
    language TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN (
            'RECEIVED',
            'TEXT_GENERATING',
            'TEXT_REVIEW',
            'TEXT_APPROVED',
            'IMAGE_GENERATING',
            'IMAGE_REVIEW',
            'READY_TO_PUBLISH',
            'PUBLISHING',
            'PUBLISHED',
            'WAITING_FOR_INPUT',
            'CANCELLED',
            'FAILED_RECOVERABLE',
            'PUBLISH_UNKNOWN'
        )
    ),
    current_text_version INTEGER NOT NULL DEFAULT 0 CHECK (current_text_version >= 0),
    current_image_version INTEGER NOT NULL DEFAULT 0 CHECK (current_image_version >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE text_versions (
    draft_id TEXT NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK (version > 0),
    body TEXT NOT NULL,
    revision_comment TEXT,
    prompt_version TEXT,
    style_version TEXT,
    provider_model TEXT,
    approved_at TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (draft_id, version)
);

CREATE TABLE image_versions (
    draft_id TEXT NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK (version > 0),
    source_text_version INTEGER NOT NULL CHECK (source_text_version > 0),
    brief TEXT NOT NULL,
    prompt TEXT,
    local_path TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    mime_type TEXT NOT NULL,
    width INTEGER NOT NULL CHECK (width > 0),
    height INTEGER NOT NULL CHECK (height > 0),
    approved_at TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (draft_id, version),
    FOREIGN KEY (draft_id, source_text_version)
        REFERENCES text_versions(draft_id, version)
);

CREATE TABLE preference_rules (
    id INTEGER PRIMARY KEY,
    scope TEXT NOT NULL CHECK (scope IN ('text', 'image')),
    rule TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('owner', 'research', 'platform', 'hypothesis')),
    version INTEGER NOT NULL CHECK (version > 0),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    draft_id TEXT REFERENCES drafts(id) ON DELETE CASCADE,
    input_version INTEGER NOT NULL CHECK (input_version >= 0),
    state TEXT NOT NULL CHECK (
        state IN ('QUEUED', 'LEASED', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'RESULT_UNKNOWN')
    ),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at TEXT NOT NULL,
    lease_owner TEXT,
    lease_expires_at TEXT,
    error_category TEXT,
    deduplication_key TEXT NOT NULL UNIQUE,
    estimated_cost_microusd INTEGER NOT NULL DEFAULT 0 CHECK (estimated_cost_microusd >= 0),
    billing_month TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (state = 'LEASED' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR state != 'LEASED'
    )
);

CREATE INDEX jobs_ready_idx ON jobs (state, next_attempt_at, id);
CREATE INDEX jobs_draft_idx ON jobs (draft_id, state);

CREATE TABLE publications (
    id INTEGER PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES drafts(id) ON DELETE RESTRICT,
    snapshot_hash TEXT NOT NULL CHECK (length(snapshot_hash) = 64),
    text_version INTEGER NOT NULL CHECK (text_version > 0),
    image_version INTEGER CHECK (image_version > 0),
    platform TEXT NOT NULL,
    account_id TEXT NOT NULL,
    visibility TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('PENDING', 'PUBLISHING', 'PUBLISHED', 'FAILED', 'PUBLISH_UNKNOWN')
    ),
    external_id TEXT,
    external_url TEXT,
    api_confirmed_at TEXT,
    readback_confirmed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (snapshot_hash, platform, account_id),
    FOREIGN KEY (draft_id, text_version)
        REFERENCES text_versions(draft_id, version),
    FOREIGN KEY (draft_id, image_version)
        REFERENCES image_versions(draft_id, version)
);

CREATE TABLE processed_updates (
    update_id INTEGER PRIMARY KEY,
    processed_at TEXT NOT NULL
);

CREATE TABLE api_usage (
    id INTEGER PRIMARY KEY,
    draft_id TEXT REFERENCES drafts(id) ON DELETE SET NULL,
    job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    stage TEXT NOT NULL,
    provider_model TEXT NOT NULL,
    input_units INTEGER CHECK (input_units IS NULL OR input_units >= 0),
    output_units INTEGER CHECK (output_units IS NULL OR output_units >= 0),
    duration_seconds TEXT,
    image_quality TEXT,
    actual_cost_microusd INTEGER CHECK (actual_cost_microusd IS NULL OR actual_cost_microusd >= 0),
    estimated_cost_microusd INTEGER NOT NULL CHECK (estimated_cost_microusd >= 0),
    currency TEXT NOT NULL DEFAULT 'USD' CHECK (currency = 'USD'),
    price_version TEXT NOT NULL,
    billing_status TEXT NOT NULL CHECK (
        billing_status IN ('ESTIMATED', 'ACTUAL', 'FAILED_BILLED', 'UNKNOWN_BILLED')
    ),
    billing_month TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX api_usage_draft_idx ON api_usage (draft_id, created_at);
CREATE INDEX api_usage_month_idx ON api_usage (billing_month, created_at);

CREATE TABLE account_connections (
    id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,
    account_id TEXT NOT NULL,
    scopes_json TEXT NOT NULL,
    encrypted_credentials BLOB NOT NULL,
    expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (platform, account_id)
);
