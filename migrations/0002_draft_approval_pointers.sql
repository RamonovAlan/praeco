ALTER TABLE drafts
    ADD COLUMN approved_text_version INTEGER CHECK (
        approved_text_version IS NULL OR approved_text_version > 0
    );

ALTER TABLE drafts
    ADD COLUMN image_source_text_version INTEGER CHECK (
        image_source_text_version IS NULL OR image_source_text_version > 0
    );

ALTER TABLE drafts
    ADD COLUMN approved_image_version INTEGER CHECK (
        approved_image_version IS NULL OR approved_image_version > 0
    );

CREATE TRIGGER drafts_approved_text_version_exists
BEFORE UPDATE OF approved_text_version ON drafts
WHEN NEW.approved_text_version IS NOT NULL
     AND NOT EXISTS (
         SELECT 1
         FROM text_versions
         WHERE draft_id = NEW.id AND version = NEW.approved_text_version
     )
BEGIN
    SELECT RAISE(ABORT, 'approved text version does not exist');
END;

CREATE TRIGGER drafts_image_source_version_exists
BEFORE UPDATE OF image_source_text_version ON drafts
WHEN NEW.image_source_text_version IS NOT NULL
     AND NOT EXISTS (
         SELECT 1
         FROM text_versions
         WHERE draft_id = NEW.id AND version = NEW.image_source_text_version
     )
BEGIN
    SELECT RAISE(ABORT, 'image source text version does not exist');
END;

CREATE TRIGGER drafts_approved_image_version_exists
BEFORE UPDATE OF approved_image_version ON drafts
WHEN NEW.approved_image_version IS NOT NULL
     AND NOT EXISTS (
         SELECT 1
         FROM image_versions
         WHERE draft_id = NEW.id AND version = NEW.approved_image_version
     )
BEGIN
    SELECT RAISE(ABORT, 'approved image version does not exist');
END;
