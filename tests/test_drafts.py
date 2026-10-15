from pathlib import Path

import pytest

from praeco.domain import DraftState
from praeco.drafts import (
    DraftIdentity,
    DraftPersistenceConflict,
    DraftRepository,
    VoiceSource,
)
from praeco.storage import apply_migrations, connect_database
from praeco.workflow import StaleVersionError, UnauthorizedActorError

MIGRATIONS = Path(__file__).parents[1] / "migrations"
OWNER_ID = 101


def repository(tmp_path: Path) -> DraftRepository:
    connection = connect_database(tmp_path / "praeco.sqlite3")
    assert apply_migrations(connection, MIGRATIONS) == (
        1,
        2,
        3,
        4,
        5,
        6,
        7,
        8,
        9,
        10,
        11,
        12,
        13,
        14,
    )
    return DraftRepository(connection)


def create_draft(drafts: DraftRepository) -> None:
    drafts.create(
        identity=DraftIdentity(
            draft_id="draft-1",
            owner_id=OWNER_ID,
            source_kind="text",
            source_text="Synthetic thought",
            language="ru",
        ),
        now="2026-10-12T12:00:00Z",
    )


def test_voice_source_accepts_one_transcript_without_exposing_file_id(
    tmp_path: Path,
) -> None:
    drafts = repository(tmp_path)
    identity = DraftIdentity(
        draft_id="voice-1",
        owner_id=OWNER_ID,
        source_kind="voice",
        source_text="",
        language="und",
        voice_source=VoiceSource(
            file_id="private-download-id",
            file_unique_id="stable-id",
            duration_seconds=12,
            file_size=500,
            mime_type="audio/ogg",
        ),
    )

    drafts.create(identity=identity, now="2026-10-12T12:00:00Z")
    drafts.record_transcript(
        draft_id="voice-1",
        actor_id=OWNER_ID,
        transcript="Synthetic transcript",
        language="ru",
        now="2026-10-12T12:00:01Z",
    )

    assert "private-download-id" not in repr(identity)
    with pytest.raises(DraftPersistenceConflict, match="already"):
        drafts.record_transcript(
            draft_id="voice-1",
            actor_id=OWNER_ID,
            transcript="Duplicate transcript",
            language="ru",
            now="2026-10-12T12:00:02Z",
        )


def make_text_ready(drafts: DraftRepository) -> None:
    create_draft(drafts)
    drafts.start_text_generation(
        draft_id="draft-1",
        actor_id=OWNER_ID,
        now="2026-10-12T12:00:01Z",
    )
    drafts.record_text(
        draft_id="draft-1",
        actor_id=OWNER_ID,
        version=1,
        body="Synthetic post",
        revision_comment=None,
        prompt_version="text-v1",
        style_version="style-v1",
        provider_model="mock-text",
        now="2026-10-12T12:00:02Z",
    )


def test_full_approval_path_survives_reload(tmp_path: Path) -> None:
    drafts = repository(tmp_path)
    make_text_ready(drafts)
    drafts.approve_text(
        draft_id="draft-1",
        actor_id=OWNER_ID,
        version=1,
        now="2026-10-12T12:00:03Z",
    )
    drafts.start_image_generation(
        draft_id="draft-1",
        actor_id=OWNER_ID,
        now="2026-10-12T12:00:04Z",
    )
    drafts.record_image(
        draft_id="draft-1",
        actor_id=OWNER_ID,
        version=1,
        source_text_version=1,
        brief="Synthetic brief",
        prompt="Synthetic image prompt",
        local_path="media/draft-1-v1.png",
        sha256="a" * 64,
        mime_type="image/png",
        width=1200,
        height=627,
        now="2026-10-12T12:00:05Z",
    )
    drafts.approve_image(
        draft_id="draft-1",
        actor_id=OWNER_ID,
        version=1,
        now="2026-10-12T12:00:06Z",
    )

    reloaded = drafts.get("draft-1")
    snapshot = drafts.publication_snapshot(draft_id="draft-1", actor_id=OWNER_ID)

    assert reloaded.state is DraftState.READY_TO_PUBLISH
    assert reloaded.approved_text_version == 1
    assert reloaded.approved_image_version == 1
    assert snapshot.text_version == 1
    assert snapshot.image_version == 1


def test_stale_approval_rolls_back_without_changing_state(tmp_path: Path) -> None:
    drafts = repository(tmp_path)
    make_text_ready(drafts)

    with pytest.raises(StaleVersionError):
        drafts.approve_text(
            draft_id="draft-1",
            actor_id=OWNER_ID,
            version=2,
            now="2026-10-12T12:00:03Z",
        )

    assert drafts.get("draft-1").state is DraftState.TEXT_REVIEW


def test_non_owner_transition_is_rolled_back(tmp_path: Path) -> None:
    drafts = repository(tmp_path)
    make_text_ready(drafts)

    with pytest.raises(UnauthorizedActorError):
        drafts.approve_text(
            draft_id="draft-1",
            actor_id=999,
            version=1,
            now="2026-10-12T12:00:03Z",
        )

    assert drafts.get("draft-1").approved_text_version is None


def test_text_revision_invalidates_persisted_approval_pointers(tmp_path: Path) -> None:
    drafts = repository(tmp_path)
    make_text_ready(drafts)
    drafts.approve_text(
        draft_id="draft-1",
        actor_id=OWNER_ID,
        version=1,
        now="2026-10-12T12:00:03Z",
    )
    drafts.start_text_generation(
        draft_id="draft-1",
        actor_id=OWNER_ID,
        now="2026-10-12T12:00:04Z",
    )
    drafts.record_text(
        draft_id="draft-1",
        actor_id=OWNER_ID,
        version=2,
        body="Revised synthetic post",
        revision_comment="Make it shorter",
        prompt_version="text-v1",
        style_version="style-v1",
        provider_model="mock-text",
        now="2026-10-12T12:00:05Z",
    )

    reloaded = drafts.get("draft-1")

    assert reloaded.state is DraftState.TEXT_REVIEW
    assert reloaded.current_text_version == 2
    assert reloaded.approved_text_version is None
    assert reloaded.approved_image_version is None
