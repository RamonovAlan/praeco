from pathlib import Path

from praeco.drafts import DraftIdentity, DraftRepository
from praeco.editing import TextEditingRepository, TextInputMode
from praeco.storage import apply_migrations, connect_database

MIGRATIONS = Path(__file__).parents[1] / "migrations"
OWNER_ID = 101
NOW = "2026-10-12T14:00:00Z"


def test_pending_text_binding_survives_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "praeco.sqlite3"
    connection = connect_database(database_path)
    apply_migrations(connection, MIGRATIONS)
    drafts = DraftRepository(connection)
    drafts.create(
        identity=DraftIdentity(
            draft_id="draft-1",
            owner_id=OWNER_ID,
            source_kind="text",
            source_text="Synthetic thought",
            language="ru",
        ),
        now=NOW,
    )
    drafts.start_text_generation(draft_id="draft-1", actor_id=OWNER_ID, now=NOW)
    drafts.record_text(
        draft_id="draft-1",
        actor_id=OWNER_ID,
        version=1,
        body="Synthetic post",
        revision_comment=None,
        prompt_version="text-v1",
        style_version="style-v1",
        provider_model="mock-text",
        image_brief="Synthetic visual brief",
        now=NOW,
    )
    drafts.wait_for_text_input(
        draft_id="draft-1",
        actor_id=OWNER_ID,
        version=1,
        now=NOW,
    )
    TextEditingRepository(connection).bind(
        owner_id=OWNER_ID,
        draft_id="draft-1",
        base_text_version=1,
        mode=TextInputMode.AI_REVISION,
        prompt_message_id=77,
        now=NOW,
    )
    connection.close()

    restarted = connect_database(database_path)
    pending = TextEditingRepository(restarted).get_pending(owner_id=OWNER_ID)

    assert pending is not None
    assert pending.draft_id == "draft-1"
    assert pending.base_text_version == 1
    assert pending.mode is TextInputMode.AI_REVISION
    assert pending.prompt_message_id == 77
    restarted.close()
