import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from praeco.budget import BudgetPolicy
from praeco.dispatcher import (
    DispatchCostEstimates,
    DispatchOutcome,
    TelegramDispatcher,
)
from praeco.domain import DraftState
from praeco.drafts import DraftRepository
from praeco.storage import apply_migrations, connect_database
from praeco.telegram import (
    TelegramCallbackInput,
    TelegramImageInput,
    TelegramInput,
    TelegramTextInput,
    TelegramVoiceInput,
)
from praeco.workflow import UnauthorizedActorError

MIGRATIONS = Path(__file__).parents[1] / "migrations"
OWNER_ID = 101


def setup(
    tmp_path: Path,
    *,
    hard_limit: str = "0.75",
    publishing_enabled: bool = False,
) -> tuple[TelegramDispatcher, sqlite3.Connection]:
    connection = connect_database(tmp_path / "praeco.sqlite3")
    apply_migrations(connection, MIGRATIONS)
    dispatcher = TelegramDispatcher(
        connection=connection,
        owner_id=OWNER_ID,
        budget_policy=BudgetPolicy(
            per_post_warning_usd=(
                Decimal("0.50")
                if hard_limit == "0.75"
                else Decimal(hard_limit) / 2
            ),
            per_post_hard_limit_usd=Decimal(hard_limit),
            monthly_hard_limit_usd=Decimal("25"),
        ),
        cost_estimates=DispatchCostEstimates(
            text_usd=Decimal("0.02"),
            transcription_usd=Decimal("0.01"),
            image_usd=Decimal("0.20"),
        ),
        media_directory=tmp_path / "media",
        publishing_enabled=publishing_enabled,
    )
    return dispatcher, connection


def text_event(*, update_id: int, text: str, command: str | None = None) -> TelegramTextInput:
    return TelegramTextInput(
        update_id=update_id,
        actor_id=OWNER_ID,
        chat_id=OWNER_ID,
        message_id=update_id,
        text=text,
        command=command,
    )


def callback_event(*, update_id: int, data: str) -> TelegramCallbackInput:
    return TelegramCallbackInput(
        update_id=update_id,
        actor_id=OWNER_ID,
        chat_id=OWNER_ID,
        message_id=update_id,
        query_id=f"query-{update_id}",
        data=data,
    )


def dispatch(dispatcher: TelegramDispatcher, event: TelegramInput):
    return dispatcher.dispatch(
        event,
        now="2026-10-12T14:00:00Z",
        billing_month="2026-10",
    )


def prepare_image_review(
    dispatcher: TelegramDispatcher,
    connection: sqlite3.Connection,
    *,
    update_id: int,
) -> str:
    draft_id = f"tg-{update_id}"
    dispatch(dispatcher, text_event(update_id=update_id, text="Synthetic thought"))
    drafts = DraftRepository(connection)
    drafts.record_text(
        draft_id=draft_id,
        actor_id=OWNER_ID,
        version=1,
        body="Synthetic post",
        revision_comment=None,
        prompt_version="text-v1",
        style_version="style-v1",
        provider_model="mock-text",
        image_brief="Synthetic visual brief",
        now="2026-10-12T14:01:00Z",
    )
    drafts.approve_text(
        draft_id=draft_id,
        actor_id=OWNER_ID,
        version=1,
        now="2026-10-12T14:02:00Z",
    )
    drafts.start_image_generation(
        draft_id=draft_id,
        actor_id=OWNER_ID,
        now="2026-10-12T14:03:00Z",
    )
    drafts.record_image(
        draft_id=draft_id,
        actor_id=OWNER_ID,
        version=1,
        source_text_version=1,
        brief="Synthetic visual brief",
        prompt="Synthetic visual prompt",
        local_path=f"{draft_id}/image-v1.png",
        sha256="a" * 64,
        mime_type="image/png",
        width=1200,
        height=1200,
        now="2026-10-12T14:04:00Z",
    )
    return draft_id


def test_command_is_claimed_without_creating_work_and_duplicate_is_ignored(
    tmp_path: Path,
) -> None:
    dispatcher, connection = setup(tmp_path)
    event = text_event(update_id=1, text="/start", command="/start")

    first = dispatch(dispatcher, event)
    duplicate = dispatch(dispatcher, event)

    assert first.outcome is DispatchOutcome.WELCOME
    assert duplicate.outcome is DispatchOutcome.DUPLICATE
    assert connection.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_linkedin_command_is_owner_only_and_creates_no_provider_work(tmp_path: Path) -> None:
    dispatcher, connection = setup(tmp_path)

    result = dispatch(
        dispatcher,
        text_event(update_id=101, text="/linkedin", command="/linkedin"),
    )

    assert result.outcome is DispatchOutcome.LINKEDIN_CONNECT
    assert connection.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_final_publish_callback_queues_exact_snapshot_once(tmp_path: Path) -> None:
    dispatcher, connection = setup(tmp_path, publishing_enabled=True)
    draft_id = prepare_image_review(dispatcher, connection, update_id=102)
    approved = dispatch(
        dispatcher,
        callback_event(update_id=103, data=f"img_ok:{draft_id}:1"),
    )
    assert approved.outcome is DispatchOutcome.IMAGE_APPROVED
    connection.execute(
        """
        INSERT INTO account_connections (
            platform, account_id, scopes_json, encrypted_credentials,
            expires_at, created_at, updated_at
        ) VALUES ('linkedin', 'member', ?, X'00', ?, ?, ?)
        """,
        (
            '["openid","profile","w_member_social"]',
            "2026-12-01T00:00:00Z",
            "2026-10-12T14:00:00Z",
            "2026-10-12T14:00:00Z",
        ),
    )
    connection.commit()
    event = callback_event(update_id=104, data=f"pub:{draft_id}:1:1")

    result = dispatch(dispatcher, event)
    duplicate = dispatch(dispatcher, event)

    assert result.outcome is DispatchOutcome.PUBLICATION_QUEUED
    assert result.publication_id is not None
    assert duplicate.outcome is DispatchOutcome.DUPLICATE
    assert DraftRepository(connection).get(draft_id).state is DraftState.PUBLISHING
    row = connection.execute(
        "SELECT draft_id, text_version, image_version, state FROM publications"
    ).fetchone()
    assert tuple(row) == (draft_id, 1, 1, "PENDING")


def test_publish_callback_is_rejected_while_feature_is_disabled(tmp_path: Path) -> None:
    dispatcher, connection = setup(tmp_path)
    draft_id = prepare_image_review(dispatcher, connection, update_id=105)
    dispatch(
        dispatcher,
        callback_event(update_id=106, data=f"img_ok:{draft_id}:1"),
    )

    result = dispatch(
        dispatcher,
        callback_event(update_id=107, data=f"pub:{draft_id}:1:1"),
    )

    assert result.outcome is DispatchOutcome.REJECTED
    assert connection.execute("SELECT COUNT(*) FROM publications").fetchone()[0] == 0


def test_text_input_atomically_creates_draft_and_one_job(tmp_path: Path) -> None:
    dispatcher, connection = setup(tmp_path)
    event = text_event(update_id=2, text="Synthetic private thought")

    result = dispatch(dispatcher, event)
    duplicate = dispatch(dispatcher, event)

    assert result.outcome is DispatchOutcome.TEXT_JOB_QUEUED
    assert result.draft_id == "tg-2"
    assert result.job_id is not None
    assert duplicate.outcome is DispatchOutcome.DUPLICATE
    draft = DraftRepository(connection).get("tg-2")
    assert draft.state is DraftState.TEXT_GENERATING
    assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_budget_block_keeps_received_draft_without_a_job(tmp_path: Path) -> None:
    dispatcher, connection = setup(tmp_path, hard_limit="0.01")

    result = dispatch(
        dispatcher,
        text_event(update_id=3, text="Synthetic private thought"),
    )

    assert result.outcome is DispatchOutcome.BUDGET_BLOCKED
    assert DraftRepository(connection).get("tg-3").state is DraftState.RECEIVED
    assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_voice_input_persists_metadata_and_queues_transcription(tmp_path: Path) -> None:
    dispatcher, connection = setup(tmp_path)
    event = TelegramVoiceInput(
        update_id=4,
        actor_id=OWNER_ID,
        chat_id=OWNER_ID,
        message_id=4,
        file_id="private-file-id",
        file_unique_id="stable-file-id",
        duration_seconds=20,
        file_size=1_000,
        mime_type="audio/ogg",
    )

    result = dispatch(dispatcher, event)

    row = connection.execute(
        """
        SELECT source_kind, source_file_unique_id, source_duration_seconds, state
        FROM drafts WHERE id = 'tg-4'
        """
    ).fetchone()
    assert result.outcome is DispatchOutcome.TRANSCRIPTION_JOB_QUEUED
    assert row["source_kind"] == "voice"
    assert row["source_file_unique_id"] == "stable-file-id"
    assert row["source_duration_seconds"] == 20
    assert row["state"] == "RECEIVED"


def test_current_text_callback_queues_image_and_duplicate_does_not(tmp_path: Path) -> None:
    dispatcher, connection = setup(tmp_path)
    dispatch(dispatcher, text_event(update_id=5, text="Synthetic thought"))
    drafts = DraftRepository(connection)
    drafts.record_text(
        draft_id="tg-5",
        actor_id=OWNER_ID,
        version=1,
        body="Synthetic post",
        revision_comment=None,
        prompt_version="text-v1",
        style_version="style-v1",
        provider_model="mock-text",
        now="2026-10-12T14:01:00Z",
    )
    event = callback_event(update_id=6, data="txt_ok:tg-5:1")

    result = dispatch(dispatcher, event)
    duplicate = dispatch(dispatcher, event)

    assert result.outcome is DispatchOutcome.IMAGE_JOB_QUEUED
    assert duplicate.outcome is DispatchOutcome.DUPLICATE
    assert drafts.get("tg-5").state is DraftState.IMAGE_GENERATING
    assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2


def test_stale_callback_is_rejected_without_partial_changes(tmp_path: Path) -> None:
    dispatcher, connection = setup(tmp_path)
    dispatch(dispatcher, text_event(update_id=7, text="Synthetic thought"))
    drafts = DraftRepository(connection)
    drafts.record_text(
        draft_id="tg-7",
        actor_id=OWNER_ID,
        version=1,
        body="First version",
        revision_comment=None,
        prompt_version="text-v1",
        style_version="style-v1",
        provider_model="mock-text",
        now="2026-10-12T14:01:00Z",
    )
    drafts.start_text_generation(
        draft_id="tg-7", actor_id=OWNER_ID, now="2026-10-12T14:02:00Z"
    )
    drafts.record_text(
        draft_id="tg-7",
        actor_id=OWNER_ID,
        version=2,
        body="Second version",
        revision_comment="Shorter",
        prompt_version="text-v1",
        style_version="style-v1",
        provider_model="mock-text",
        now="2026-10-12T14:03:00Z",
    )

    result = dispatch(
        dispatcher,
        callback_event(update_id=8, data="txt_ok:tg-7:1"),
    )

    assert result.outcome is DispatchOutcome.REJECTED
    assert drafts.get("tg-7").state is DraftState.TEXT_REVIEW
    assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_constructed_non_owner_event_is_rejected_before_claim(tmp_path: Path) -> None:
    dispatcher, connection = setup(tmp_path)
    event = TelegramTextInput(
        update_id=9,
        actor_id=202,
        chat_id=202,
        message_id=9,
        text="Untrusted",
    )

    with pytest.raises(UnauthorizedActorError):
        dispatch(dispatcher, event)

    assert connection.execute("SELECT COUNT(*) FROM processed_updates").fetchone()[0] == 0


def test_manual_edit_creates_version_without_a_paid_job(tmp_path: Path) -> None:
    dispatcher, connection = setup(tmp_path)
    dispatch(dispatcher, text_event(update_id=10, text="Synthetic thought"))
    drafts = DraftRepository(connection)
    drafts.record_text(
        draft_id="tg-10",
        actor_id=OWNER_ID,
        version=1,
        body="First post",
        revision_comment=None,
        prompt_version="text-v1",
        style_version="style-v1",
        provider_model="mock-text",
        image_brief="First visual brief",
        now="2026-10-12T14:01:00Z",
    )

    requested = dispatch(
        dispatcher,
        callback_event(update_id=11, data="txt_edit:tg-10:1"),
    )
    edited = dispatch(
        dispatcher,
        TelegramTextInput(
            update_id=12,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=12,
            text="Manually edited post",
            reply_to_message_id=11,
        ),
    )

    assert requested.outcome is DispatchOutcome.MANUAL_EDIT_REQUESTED
    assert edited.outcome is DispatchOutcome.TEXT_MANUALLY_EDITED
    assert drafts.get("tg-10").state is DraftState.TEXT_REVIEW
    assert drafts.get("tg-10").current_text_version == 2
    version = drafts.get_text_version(draft_id="tg-10", version=2)
    assert version.body == "Manually edited post"
    assert version.image_brief == "Manually edited post"
    assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM api_usage").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM pending_text_inputs").fetchone()[0] == 0
    delivery = connection.execute(
        "SELECT kind, text_version, state FROM telegram_outbox"
    ).fetchone()
    assert tuple(delivery) == ("TEXT_REVIEW", 2, "PENDING")

    stale = dispatch(
        dispatcher,
        callback_event(update_id=13, data="txt_rev:tg-10:1"),
    )
    assert stale.outcome is DispatchOutcome.REJECTED
    assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_revision_binding_rejects_a_reply_to_another_message(tmp_path: Path) -> None:
    dispatcher, connection = setup(tmp_path)
    dispatch(dispatcher, text_event(update_id=20, text="Synthetic thought"))
    drafts = DraftRepository(connection)
    drafts.record_text(
        draft_id="tg-20",
        actor_id=OWNER_ID,
        version=1,
        body="First post",
        revision_comment=None,
        prompt_version="text-v1",
        style_version="style-v1",
        provider_model="mock-text",
        image_brief="First visual brief",
        now="2026-10-12T14:01:00Z",
    )
    dispatch(
        dispatcher,
        callback_event(update_id=21, data="txt_rev:tg-20:1"),
    )

    result = dispatch(
        dispatcher,
        TelegramTextInput(
            update_id=22,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=22,
            text="This belongs elsewhere",
            reply_to_message_id=999,
        ),
    )

    assert result.outcome is DispatchOutcome.REJECTED
    assert drafts.get("tg-20").state is DraftState.WAITING_FOR_INPUT
    assert connection.execute("SELECT COUNT(*) FROM pending_text_inputs").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 1


def test_second_pending_draft_is_rejected_without_stranding_it(tmp_path: Path) -> None:
    dispatcher, connection = setup(tmp_path)
    drafts = DraftRepository(connection)
    for update_id in (30, 31):
        dispatch(dispatcher, text_event(update_id=update_id, text="Synthetic thought"))
        drafts.record_text(
            draft_id=f"tg-{update_id}",
            actor_id=OWNER_ID,
            version=1,
            body="First post",
            revision_comment=None,
            prompt_version="text-v1",
            style_version="style-v1",
            provider_model="mock-text",
            image_brief="First visual brief",
            now="2026-10-12T14:01:00Z",
        )
    first = dispatch(
        dispatcher,
        callback_event(update_id=32, data="txt_rev:tg-30:1"),
    )
    second = dispatch(
        dispatcher,
        callback_event(update_id=33, data="txt_edit:tg-31:1"),
    )

    assert first.outcome is DispatchOutcome.TEXT_REVISION_REQUESTED
    assert second.outcome is DispatchOutcome.REJECTED
    assert drafts.get("tg-30").state is DraftState.WAITING_FOR_INPUT
    assert drafts.get("tg-31").state is DraftState.TEXT_REVIEW
    pending = connection.execute(
        "SELECT draft_id, mode FROM pending_text_inputs"
    ).fetchone()
    assert tuple(pending) == ("tg-30", "AI_REVISION")


def test_new_command_abandons_edit_binding_before_the_next_thought(tmp_path: Path) -> None:
    dispatcher, connection = setup(tmp_path)
    drafts = DraftRepository(connection)
    dispatch(dispatcher, text_event(update_id=50, text="First thought"))
    drafts.record_text(
        draft_id="tg-50",
        actor_id=OWNER_ID,
        version=1,
        body="First post",
        revision_comment=None,
        prompt_version="text-v1",
        style_version="style-v1",
        provider_model="mock-text",
        image_brief="First visual brief",
        now="2026-10-12T14:01:00Z",
    )
    dispatch(
        dispatcher,
        callback_event(update_id=51, data="txt_edit:tg-50:1"),
    )

    reset = dispatch(
        dispatcher,
        text_event(update_id=52, text="/new", command="/new"),
    )
    new_thought = dispatch(
        dispatcher,
        text_event(update_id=53, text="A genuinely new thought"),
    )

    assert reset.outcome is DispatchOutcome.WAITING_FOR_INPUT
    assert new_thought.outcome is DispatchOutcome.TEXT_JOB_QUEUED
    assert drafts.get("tg-50").state is DraftState.TEXT_REVIEW
    assert drafts.get("tg-53").state is DraftState.TEXT_GENERATING
    assert connection.execute("SELECT COUNT(*) FROM pending_text_inputs").fetchone()[0] == 0


def test_budget_block_keeps_revision_binding_without_creating_a_job(
    tmp_path: Path,
) -> None:
    dispatcher, connection = setup(tmp_path, hard_limit="0.03")
    drafts = DraftRepository(connection)
    dispatch(dispatcher, text_event(update_id=60, text="Synthetic thought"))
    drafts.record_text(
        draft_id="tg-60",
        actor_id=OWNER_ID,
        version=1,
        body="First post",
        revision_comment=None,
        prompt_version="text-v1",
        style_version="style-v1",
        provider_model="mock-text",
        image_brief="First visual brief",
        now="2026-10-12T14:01:00Z",
    )
    dispatch(
        dispatcher,
        callback_event(update_id=61, data="txt_rev:tg-60:1"),
    )

    result = dispatch(
        dispatcher,
        TelegramTextInput(
            update_id=62,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=62,
            text="Make it shorter",
            reply_to_message_id=61,
        ),
    )

    assert result.outcome is DispatchOutcome.BUDGET_BLOCKED
    assert drafts.get("tg-60").state is DraftState.WAITING_FOR_INPUT
    assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM text_revision_requests").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM pending_text_inputs").fetchone()[0] == 1


def test_image_revision_comment_is_bound_and_queues_one_new_image_job(
    tmp_path: Path,
) -> None:
    dispatcher, connection = setup(tmp_path)
    draft_id = prepare_image_review(
        dispatcher,
        connection,
        update_id=70,
    )

    requested = dispatch(
        dispatcher,
        callback_event(update_id=71, data=f"img_rev:{draft_id}:1"),
    )
    queued = dispatch(
        dispatcher,
        TelegramTextInput(
            update_id=72,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=72,
            text="Make the background darker",
            reply_to_message_id=71,
        ),
    )

    assert requested.outcome is DispatchOutcome.IMAGE_REVISION_REQUESTED
    assert queued.outcome is DispatchOutcome.IMAGE_REVISION_JOB_QUEUED
    assert DraftRepository(connection).get(draft_id).state is DraftState.IMAGE_GENERATING
    request = connection.execute(
        """
        SELECT base_image_version, source_text_version, comment
        FROM image_revision_requests
        """
    ).fetchone()
    assert tuple(request) == (1, 1, "Make the background darker")
    assert connection.execute("SELECT COUNT(*) FROM pending_image_inputs").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2


def test_owner_upload_button_binds_without_starting_generation(tmp_path: Path) -> None:
    dispatcher, connection = setup(tmp_path)
    draft_id = prepare_image_review(
        dispatcher,
        connection,
        update_id=80,
    )

    requested = dispatch(
        dispatcher,
        callback_event(update_id=81, data=f"img_upload:{draft_id}:1"),
    )
    wrong_input = dispatch(
        dispatcher,
        TelegramTextInput(
            update_id=82,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=82,
            text="This is not an image",
            reply_to_message_id=81,
        ),
    )

    assert requested.outcome is DispatchOutcome.OWNER_IMAGE_UPLOAD_REQUESTED
    assert wrong_input.outcome is DispatchOutcome.OWNER_IMAGE_UPLOAD_EXPECTED
    assert DraftRepository(connection).get(draft_id).state is DraftState.WAITING_FOR_INPUT
    pending = connection.execute(
        "SELECT draft_id, base_image_version, mode FROM pending_image_inputs"
    ).fetchone()
    assert tuple(pending) == (draft_id, 1, "OWNER_UPLOAD")
    assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_bound_text_voice_revision_reserves_ordered_jobs_without_new_draft(
    tmp_path: Path,
) -> None:
    dispatcher, connection = setup(tmp_path)
    dispatch(dispatcher, text_event(update_id=85, text="Synthetic thought"))
    drafts = DraftRepository(connection)
    drafts.record_text(
        draft_id="tg-85",
        actor_id=OWNER_ID,
        version=1,
        body="First post",
        revision_comment=None,
        prompt_version="text-v1",
        style_version="style-v1",
        provider_model="mock-text",
        image_brief="First visual brief",
        now="2026-10-12T14:01:00Z",
    )
    dispatch(
        dispatcher,
        callback_event(update_id=86, data="txt_rev:tg-85:1"),
    )

    queued = dispatch(
        dispatcher,
        TelegramVoiceInput(
            update_id=87,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=87,
            file_id="private-file-id",
            file_unique_id="stable-revision-voice",
            duration_seconds=12,
            file_size=120,
            mime_type="audio/ogg",
            reply_to_message_id=86,
        ),
    )
    competing = dispatch(
        dispatcher,
        text_event(update_id=88, text="Do not apply this concurrently"),
    )

    assert queued.outcome is DispatchOutcome.REVISION_TRANSCRIPTION_JOB_QUEUED
    assert competing.outcome is DispatchOutcome.REJECTED
    assert connection.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3
    voice = connection.execute(
        """
        SELECT target_kind, draft_id, base_text_version, base_image_version,
               telegram_update_id
        FROM voice_revision_inputs
        """
    ).fetchone()
    assert tuple(voice) == ("TEXT", "tg-85", 1, None, 87)
    dependency = connection.execute(
        "SELECT job_id, prerequisite_job_id FROM job_dependencies"
    ).fetchone()
    assert dependency["job_id"] > dependency["prerequisite_job_id"]
    assert connection.execute("SELECT COUNT(*) FROM pending_text_inputs").fetchone()[0] == 1


def test_voice_revision_budget_block_rolls_back_both_reservations(
    tmp_path: Path,
) -> None:
    dispatcher, connection = setup(tmp_path, hard_limit="0.04")
    dispatch(dispatcher, text_event(update_id=95, text="Synthetic thought"))
    drafts = DraftRepository(connection)
    drafts.record_text(
        draft_id="tg-95",
        actor_id=OWNER_ID,
        version=1,
        body="First post",
        revision_comment=None,
        prompt_version="text-v1",
        style_version="style-v1",
        provider_model="mock-text",
        image_brief="First visual brief",
        now="2026-10-12T14:01:00Z",
    )
    dispatch(
        dispatcher,
        callback_event(update_id=96, data="txt_rev:tg-95:1"),
    )

    result = dispatch(
        dispatcher,
        TelegramVoiceInput(
            update_id=97,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=97,
            file_id="private-file-id",
            file_unique_id="blocked-revision-voice",
            duration_seconds=12,
            file_size=120,
            mime_type="audio/ogg",
            reply_to_message_id=96,
        ),
    )

    assert result.outcome is DispatchOutcome.BUDGET_BLOCKED
    assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM job_dependencies").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM voice_revision_inputs").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM pending_text_inputs").fetchone()[0] == 1


def test_return_to_text_keeps_image_history_and_enqueues_exact_text(tmp_path: Path) -> None:
    dispatcher, connection = setup(tmp_path)
    draft_id = prepare_image_review(
        dispatcher,
        connection,
        update_id=90,
    )

    result = dispatch(
        dispatcher,
        callback_event(update_id=91, data=f"img_text:{draft_id}:1"),
    )

    workflow = DraftRepository(connection).get(draft_id)
    assert result.outcome is DispatchOutcome.RETURNED_TO_TEXT
    assert workflow.state is DraftState.TEXT_REVIEW
    assert workflow.current_image_version == 1
    assert workflow.approved_text_version is None
    delivery = connection.execute(
        "SELECT kind, text_version, image_version, state FROM telegram_outbox"
    ).fetchone()
    assert tuple(delivery) == ("TEXT_REVIEW", 1, None, "PENDING")


def test_image_cancel_checks_image_version_instead_of_text_version(tmp_path: Path) -> None:
    dispatcher, connection = setup(tmp_path)
    draft_id = prepare_image_review(
        dispatcher,
        connection,
        update_id=100,
    )
    drafts = DraftRepository(connection)
    drafts.start_image_generation(
        draft_id=draft_id,
        actor_id=OWNER_ID,
        now="2026-10-12T14:05:00Z",
    )
    drafts.record_image(
        draft_id=draft_id,
        actor_id=OWNER_ID,
        version=2,
        source_text_version=1,
        brief="Synthetic visual brief",
        prompt="Second prompt",
        local_path=f"{draft_id}/image-v2.png",
        sha256="b" * 64,
        mime_type="image/png",
        width=1200,
        height=1200,
        now="2026-10-12T14:06:00Z",
    )

    stale_text_cancel = dispatch(
        dispatcher,
        callback_event(update_id=101, data=f"txt_cancel:{draft_id}:2"),
    )
    image_cancel = dispatch(
        dispatcher,
        callback_event(update_id=102, data=f"img_cancel:{draft_id}:2"),
    )

    assert stale_text_cancel.outcome is DispatchOutcome.REJECTED
    assert image_cancel.outcome is DispatchOutcome.CANCELLED
    assert drafts.get(draft_id).state is DraftState.CANCELLED


def test_owner_image_upload_stores_immutable_version_without_a_paid_job(
    tmp_path: Path,
) -> None:
    dispatcher, connection = setup(tmp_path)
    draft_id = prepare_image_review(
        dispatcher,
        connection,
        update_id=110,
    )
    dispatch(
        dispatcher,
        callback_event(update_id=111, data=f"img_upload:{draft_id}:1"),
    )
    staged_path = tmp_path / "media" / ".staging" / "owner-image-112.png"
    staged_path.parent.mkdir(parents=True)
    content = synthetic_png()
    staged_path.write_bytes(content)
    event = TelegramImageInput(
        update_id=112,
        actor_id=OWNER_ID,
        chat_id=OWNER_ID,
        message_id=112,
        file_id="private-file-id",
        file_unique_id="stable-file-id",
        file_size=len(content),
        mime_type="image/png",
        width=800,
        height=600,
        reply_to_message_id=111,
        staged_path=staged_path,
    )

    stored = dispatch(dispatcher, event)
    duplicate = dispatch(dispatcher, event)

    assert stored.outcome is DispatchOutcome.OWNER_IMAGE_STORED
    assert duplicate.outcome is DispatchOutcome.DUPLICATE
    workflow = DraftRepository(connection).get(draft_id)
    assert workflow.state is DraftState.IMAGE_REVIEW
    assert workflow.current_image_version == 2
    image = connection.execute(
        """
        SELECT source_text_version, prompt, local_path, mime_type, width, height
        FROM image_versions WHERE draft_id = ? AND version = 2
        """,
        (draft_id,),
    ).fetchone()
    assert tuple(image) == (
        1,
        None,
        f"{draft_id}/owner-v2.png",
        "image/png",
        800,
        600,
    )
    final_path = tmp_path / "media" / image["local_path"]
    assert final_path.read_bytes() == content
    assert final_path.stat().st_mode & 0o777 == 0o600
    assert final_path.parent.stat().st_mode & 0o777 == 0o700
    assert not staged_path.exists()
    assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM api_usage").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM pending_image_inputs").fetchone()[0] == 0
    delivery = connection.execute(
        "SELECT kind, image_version, state FROM telegram_outbox"
    ).fetchone()
    assert tuple(delivery) == ("IMAGE_REVIEW", 2, "PENDING")


def synthetic_png(*, width: int = 800, height: int = 600) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
        + b"\x00\x00\x00\x00"
    )
