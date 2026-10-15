CREATE TABLE pending_image_inputs_new (
    owner_id INTEGER PRIMARY KEY CHECK (owner_id > 0),
    draft_id TEXT NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    base_image_version INTEGER NOT NULL CHECK (base_image_version >= 0),
    source_text_version INTEGER NOT NULL CHECK (source_text_version > 0),
    mode TEXT NOT NULL CHECK (mode IN ('AI_REVISION', 'OWNER_UPLOAD')),
    prompt_message_id INTEGER NOT NULL CHECK (prompt_message_id >= 0),
    created_at TEXT NOT NULL,
    FOREIGN KEY (draft_id, source_text_version)
        REFERENCES text_versions(draft_id, version),
    CHECK (mode = 'OWNER_UPLOAD' OR base_image_version > 0)
);

INSERT INTO pending_image_inputs_new (
    owner_id, draft_id, base_image_version, source_text_version,
    mode, prompt_message_id, created_at
)
SELECT owner_id, draft_id, base_image_version, source_text_version,
       mode, prompt_message_id, created_at
FROM pending_image_inputs;

DROP TABLE pending_image_inputs;
ALTER TABLE pending_image_inputs_new RENAME TO pending_image_inputs;
