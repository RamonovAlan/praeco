import hashlib
import io
import json
import zipfile
from pathlib import Path

from praeco.drafts import DraftIdentity, DraftRepository
from praeco.owner import OwnerExporter, OwnerRepository
from praeco.storage import apply_migrations, connect_database

MIGRATIONS = Path(__file__).parents[1] / "migrations"
OWNER_ID = 101
NOW = "2026-10-15T08:00:00Z"


def test_owner_export_contains_content_and_media_but_no_oauth_credentials(
    tmp_path: Path,
) -> None:
    connection = connect_database(tmp_path / "praeco.sqlite3")
    apply_migrations(connection, MIGRATIONS)
    media = tmp_path / "media"
    image = media / "draft-1" / "image-v1.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"synthetic-image")
    drafts = DraftRepository(connection)
    drafts.create(
        identity=DraftIdentity(
            draft_id="draft-1",
            owner_id=OWNER_ID,
            source_kind="text",
            source_text="Private source thought",
            language="ru",
        ),
        now=NOW,
    )
    drafts.start_text_generation(draft_id="draft-1", actor_id=OWNER_ID, now=NOW)
    drafts.record_text(
        draft_id="draft-1",
        actor_id=OWNER_ID,
        version=1,
        body="Private generated text",
        revision_comment=None,
        prompt_version="text-v1",
        style_version="style-v1",
        provider_model="mock",
        image_brief="Synthetic brief",
        now=NOW,
    )
    drafts.approve_text(
        draft_id="draft-1",
        actor_id=OWNER_ID,
        version=1,
        now=NOW,
    )
    drafts.start_image_generation(draft_id="draft-1", actor_id=OWNER_ID, now=NOW)
    drafts.record_image(
        draft_id="draft-1",
        actor_id=OWNER_ID,
        version=1,
        source_text_version=1,
        brief="Synthetic brief",
        prompt="Synthetic prompt",
        local_path="draft-1/image-v1.png",
        sha256=hashlib.sha256(b"synthetic-image").hexdigest(),
        mime_type="image/png",
        width=1200,
        height=800,
        now=NOW,
    )
    connection.execute(
        """
        INSERT INTO account_connections (
            platform, account_id, scopes_json, encrypted_credentials,
            expires_at, created_at, updated_at
        ) VALUES ('linkedin', 'member', '["w_member_social"]', ?, NULL, ?, ?)
        """,
        (b"must-not-export", NOW, NOW),
    )
    connection.commit()

    content = OwnerExporter(
        connection=connection,
        media_directory=media,
    ).build_zip(owner_id=OWNER_ID, max_bytes=1_000_000)

    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        assert archive.namelist() == [
            "praeco-data.json",
            "images/draft-1/v1.png",
        ]
        exported = archive.read("praeco-data.json")
        payload = json.loads(exported)
        assert payload["drafts"][0]["source_text"] == "Private source thought"
        assert payload["account_connections"][0]["platform"] == "linkedin"
        assert b"must-not-export" not in content
        assert b"encrypted_credentials" not in exported
        assert archive.read("images/draft-1/v1.png") == b"synthetic-image"


def test_owner_deletion_plan_removes_database_content_but_keeps_update_dedup(
    tmp_path: Path,
) -> None:
    connection = connect_database(tmp_path / "praeco.sqlite3")
    apply_migrations(connection, MIGRATIONS)
    drafts = DraftRepository(connection)
    drafts.create(
        identity=DraftIdentity(
            draft_id="draft-delete",
            owner_id=OWNER_ID,
            source_kind="text",
            source_text="Private source thought",
            language="ru",
        ),
        now=NOW,
    )
    connection.execute(
        "INSERT INTO processed_updates (update_id, processed_at) VALUES (1, ?)",
        (NOW,),
    )
    connection.commit()

    plan = OwnerRepository(connection).delete_all_owner_data(owner_id=OWNER_ID)

    assert plan.draft_ids == ("draft-delete",)
    assert connection.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM processed_updates").fetchone()[0] == 1
