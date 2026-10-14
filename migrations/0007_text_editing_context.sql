CREATE TABLE pending_text_inputs (
    owner_id INTEGER PRIMARY KEY CHECK (owner_id > 0),
    draft_id TEXT NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    base_text_version INTEGER NOT NULL CHECK (base_text_version > 0),
    mode TEXT NOT NULL CHECK (mode IN ('AI_REVISION', 'MANUAL_EDIT')),
    prompt_message_id INTEGER NOT NULL CHECK (prompt_message_id >= 0),
    created_at TEXT NOT NULL,
    FOREIGN KEY (draft_id, base_text_version)
        REFERENCES text_versions(draft_id, version)
);

CREATE TABLE text_revision_requests (
    job_id INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    draft_id TEXT NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    base_text_version INTEGER NOT NULL CHECK (base_text_version > 0),
    comment TEXT NOT NULL CHECK (length(trim(comment)) > 0),
    source_kind TEXT NOT NULL CHECK (source_kind IN ('text', 'voice')),
    created_at TEXT NOT NULL,
    FOREIGN KEY (draft_id, base_text_version)
        REFERENCES text_versions(draft_id, version)
);

CREATE INDEX text_revision_requests_draft_idx
ON text_revision_requests (draft_id, base_text_version);
