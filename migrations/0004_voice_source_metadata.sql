ALTER TABLE drafts ADD COLUMN source_file_id TEXT;
ALTER TABLE drafts ADD COLUMN source_file_unique_id TEXT;
ALTER TABLE drafts ADD COLUMN source_mime_type TEXT;
ALTER TABLE drafts ADD COLUMN source_file_size INTEGER CHECK (
    source_file_size IS NULL OR source_file_size >= 0
);
ALTER TABLE drafts ADD COLUMN source_duration_seconds INTEGER CHECK (
    source_duration_seconds IS NULL OR source_duration_seconds >= 0
);

CREATE TRIGGER drafts_source_insert_valid
BEFORE INSERT ON drafts
WHEN (
    NEW.source_kind = 'text'
    AND (
        length(trim(NEW.source_text)) = 0
        OR NEW.source_file_id IS NOT NULL
        OR NEW.source_file_unique_id IS NOT NULL
        OR NEW.source_duration_seconds IS NOT NULL
    )
) OR (
    NEW.source_kind = 'voice'
    AND (
        NEW.source_file_id IS NULL
        OR length(trim(NEW.source_file_id)) = 0
        OR NEW.source_file_unique_id IS NULL
        OR length(trim(NEW.source_file_unique_id)) = 0
        OR NEW.source_duration_seconds IS NULL
    )
)
BEGIN
    SELECT RAISE(ABORT, 'invalid draft source metadata');
END;

CREATE TRIGGER drafts_source_update_valid
BEFORE UPDATE OF source_kind, source_text, source_file_id,
                 source_file_unique_id, source_duration_seconds ON drafts
WHEN (
    NEW.source_kind = 'text'
    AND (
        length(trim(NEW.source_text)) = 0
        OR NEW.source_file_id IS NOT NULL
        OR NEW.source_file_unique_id IS NOT NULL
        OR NEW.source_duration_seconds IS NOT NULL
    )
) OR (
    NEW.source_kind = 'voice'
    AND (
        NEW.source_file_id IS NULL
        OR length(trim(NEW.source_file_id)) = 0
        OR NEW.source_file_unique_id IS NULL
        OR length(trim(NEW.source_file_unique_id)) = 0
        OR NEW.source_duration_seconds IS NULL
    )
)
BEGIN
    SELECT RAISE(ABORT, 'invalid draft source metadata');
END;
