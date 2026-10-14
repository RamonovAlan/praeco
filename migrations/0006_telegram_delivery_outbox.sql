CREATE TABLE telegram_outbox (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('TEXT_REVIEW', 'IMAGE_REVIEW')),
    draft_id TEXT NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    chat_id INTEGER NOT NULL,
    text_version INTEGER CHECK (text_version IS NULL OR text_version > 0),
    image_version INTEGER CHECK (image_version IS NULL OR image_version > 0),
    state TEXT NOT NULL CHECK (state IN ('PENDING', 'LEASED', 'SENT', 'FAILED')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at TEXT NOT NULL,
    lease_owner TEXT,
    lease_expires_at TEXT,
    error_category TEXT,
    telegram_message_id INTEGER,
    deduplication_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    sent_at TEXT,
    FOREIGN KEY (draft_id, text_version)
        REFERENCES text_versions(draft_id, version),
    FOREIGN KEY (draft_id, image_version)
        REFERENCES image_versions(draft_id, version),
    CHECK (
        (kind = 'TEXT_REVIEW' AND text_version IS NOT NULL AND image_version IS NULL)
        OR
        (kind = 'IMAGE_REVIEW' AND text_version IS NULL AND image_version IS NOT NULL)
    ),
    CHECK (
        (state = 'LEASED' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR state != 'LEASED'
    ),
    CHECK (
        (state = 'SENT' AND telegram_message_id IS NOT NULL AND sent_at IS NOT NULL)
        OR state != 'SENT'
    )
);

CREATE INDEX telegram_outbox_ready_idx
ON telegram_outbox (state, next_attempt_at, id);

CREATE INDEX telegram_outbox_draft_idx
ON telegram_outbox (draft_id, state);
