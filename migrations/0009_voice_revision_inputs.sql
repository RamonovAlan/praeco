CREATE TABLE job_dependencies (
    job_id INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    prerequisite_job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    CHECK (job_id != prerequisite_job_id)
);

CREATE INDEX job_dependencies_prerequisite_idx
ON job_dependencies (prerequisite_job_id);

CREATE TABLE voice_revision_inputs (
    transcription_job_id INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    generation_job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
    owner_id INTEGER NOT NULL CHECK (owner_id > 0),
    draft_id TEXT NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    target_kind TEXT NOT NULL CHECK (target_kind IN ('TEXT', 'IMAGE')),
    base_text_version INTEGER NOT NULL CHECK (base_text_version > 0),
    base_image_version INTEGER CHECK (base_image_version > 0),
    prompt_message_id INTEGER NOT NULL CHECK (prompt_message_id >= 0),
    telegram_update_id INTEGER NOT NULL UNIQUE CHECK (telegram_update_id >= 0),
    file_unique_id TEXT NOT NULL CHECK (length(trim(file_unique_id)) > 0),
    duration_seconds INTEGER NOT NULL CHECK (duration_seconds >= 0),
    file_size INTEGER CHECK (file_size IS NULL OR file_size >= 0),
    mime_type TEXT NOT NULL CHECK (length(trim(mime_type)) > 0),
    created_at TEXT NOT NULL,
    FOREIGN KEY (draft_id, base_text_version)
        REFERENCES text_versions(draft_id, version),
    FOREIGN KEY (draft_id, base_image_version)
        REFERENCES image_versions(draft_id, version),
    CHECK (
        (target_kind = 'TEXT' AND base_image_version IS NULL)
        OR (target_kind = 'IMAGE' AND base_image_version IS NOT NULL)
    )
);

CREATE INDEX voice_revision_inputs_owner_idx
ON voice_revision_inputs (owner_id, transcription_job_id);
