from decimal import Decimal
from pathlib import Path

import pytest

from praeco.budget import BudgetPolicy
from praeco.dispatcher import DispatchCostEstimates, DispatchOutcome, TelegramDispatcher
from praeco.domain import DraftState
from praeco.drafts import DraftRepository
from praeco.outbox import DeliveryKind, DeliveryRepository
from praeco.owner import OwnerRepository
from praeco.providers import (
    ImageGenerationResult,
    MockImageProvider,
    MockTextProvider,
    MockTranscriptionProvider,
    ProviderCallError,
    ProviderMetering,
    TextGenerationResult,
    TranscriptionResult,
)
from praeco.storage import apply_migrations, connect_database
from praeco.telegram import TelegramCallbackInput, TelegramTextInput, TelegramVoiceInput
from praeco.worker import (
    SingleWorker,
    WorkerOutcome,
    recover_orphaned_text_calls,
    staged_voice_path,
)

MIGRATIONS = Path(__file__).parents[1] / "migrations"
OWNER_ID = 101
NOW = "2026-10-12T15:00:00Z"


def policy() -> BudgetPolicy:
    return BudgetPolicy(
        per_post_warning_usd=Decimal("0.50"),
        per_post_hard_limit_usd=Decimal("0.75"),
        monthly_hard_limit_usd=Decimal("25"),
    )


def setup(tmp_path: Path):
    connection = connect_database(tmp_path / "praeco.sqlite3")
    apply_migrations(connection, MIGRATIONS)
    text_provider = MockTextProvider(
        TextGenerationResult(
            body="Synthetic generated post",
            language="ru",
            image_brief="A synthetic dimensional scene",
            metering=ProviderMetering(
                provider_model="mock-text",
                price_version="2026-10-12",
                actual_cost_usd=Decimal("0.01"),
                input_units=100,
                output_units=50,
            ),
        )
    )
    transcription_provider = MockTranscriptionProvider(
        TranscriptionResult(
            transcript="Synthetic voice transcript",
            language="ru",
            metering=ProviderMetering(
                provider_model="mock-transcription",
                price_version="2026-10-12",
                actual_cost_usd=Decimal("0.005"),
                duration_seconds=Decimal("10"),
            ),
        )
    )
    image_provider = MockImageProvider(
        ImageGenerationResult(
            content=b"synthetic-png",
            mime_type="image/png",
            width=1200,
            height=1200,
            metering=ProviderMetering(
                provider_model="mock-image",
                price_version="2026-10-12",
                actual_cost_usd=Decimal("0.04"),
                image_quality="medium",
            ),
        )
    )
    dispatcher = TelegramDispatcher(
        connection=connection,
        owner_id=OWNER_ID,
        budget_policy=policy(),
        cost_estimates=DispatchCostEstimates(
            text_usd=Decimal("0.02"),
            transcription_usd=Decimal("0.01"),
            image_usd=Decimal("0.05"),
        ),
    )
    worker = SingleWorker(
        connection=connection,
        owner_id=OWNER_ID,
        media_directory=tmp_path / "media",
        text_provider=text_provider,
        transcription_provider=transcription_provider,
        image_provider=image_provider,
        budget_policy=policy(),
        next_text_estimate_usd=Decimal("0.02"),
    )
    return connection, dispatcher, worker, text_provider, transcription_provider, image_provider


def run(worker: SingleWorker):
    return worker.run_once(
        worker_id="worker-1",
        now=NOW,
        lease_expires_at="2026-10-12T15:05:00Z",
    )


def test_text_result_is_persisted_and_billed_before_delivery(tmp_path: Path) -> None:
    connection, dispatcher, worker, text_provider, _, _ = setup(tmp_path)
    dispatcher.dispatch(
        TelegramTextInput(
            update_id=1,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=1,
            text="Synthetic thought",
        ),
        now=NOW,
        billing_month="2026-10",
    )

    result = run(worker)

    assert result.outcome is WorkerOutcome.TEXT_STORED
    assert text_provider.calls == 1
    assert DraftRepository(connection).get("tg-1").state is DraftState.TEXT_REVIEW
    text_version = connection.execute(
        "SELECT prompt_version, style_version FROM text_versions "
        "WHERE draft_id = ? AND version = 1",
        ("tg-1",),
    ).fetchone()
    job = connection.execute("SELECT state FROM jobs WHERE id = ?", (result.job_id,)).fetchone()
    usage = connection.execute(
        "SELECT billing_status, actual_cost_microusd FROM api_usage WHERE job_id = ?",
        (result.job_id,),
    ).fetchone()
    assert job["state"] == "SUCCEEDED"
    assert text_version["prompt_version"] == "text-v0.3"
    assert text_version["style_version"] == "style-v2"
    assert usage["billing_status"] == "ACTUAL"
    assert usage["actual_cost_microusd"] == 10_000


def test_active_owner_rules_are_bound_to_future_text_and_image_calls(
    tmp_path: Path,
) -> None:
    connection, dispatcher, worker, text_provider, _, image_provider = setup(tmp_path)
    owner = OwnerRepository(connection)
    owner.add_preference(
        scope="text",
        rule="Use short paragraphs",
        now=NOW,
    )
    owner.add_preference(
        scope="image",
        rule="Use a warm neutral background",
        now=NOW,
    )
    dispatcher.dispatch(
        TelegramTextInput(
            update_id=90,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=90,
            text="Synthetic thought",
        ),
        now=NOW,
        billing_month="2026-10",
    )

    run(worker)

    assert text_provider.requests[0].style_rules == ("Use short paragraphs",)
    style_version = connection.execute(
        "SELECT style_version FROM text_versions WHERE draft_id = 'tg-90'"
    ).fetchone()[0]
    assert style_version.startswith("style-v2+owner-")
    dispatcher.dispatch(
        TelegramCallbackInput(
            update_id=91,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=91,
            query_id="query-91",
            data="txt_ok:tg-90:1",
        ),
        now=NOW,
        billing_month="2026-10",
    )

    run(worker)

    assert "Use a warm neutral background" in image_provider.requests[0].prompt


def test_voice_transcript_persists_then_queues_exactly_one_text_job(tmp_path: Path) -> None:
    connection, dispatcher, worker, _, transcription_provider, _ = setup(tmp_path)
    dispatcher.dispatch(
        TelegramVoiceInput(
            update_id=2,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=2,
            file_id="private-file-id",
            file_unique_id="stable-file-id",
            duration_seconds=10,
            file_size=100,
            mime_type="audio/ogg",
        ),
        now=NOW,
        billing_month="2026-10",
    )
    voice_path = staged_voice_path(
        media_directory=tmp_path / "media",
        draft_id="tg-2",
    )
    voice_path.parent.mkdir(parents=True)
    voice_path.write_bytes(b"synthetic-audio")

    transcription = run(worker)
    text = run(worker)

    assert transcription.outcome is WorkerOutcome.TRANSCRIPT_STORED
    assert transcription.next_job_id is not None
    assert text.outcome is WorkerOutcome.TEXT_STORED
    assert transcription_provider.calls == 1
    assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
    source_text = connection.execute(
        "SELECT source_text FROM drafts WHERE id = 'tg-2'"
    ).fetchone()[0]
    assert source_text == "Synthetic voice transcript"


def test_image_original_is_stored_before_job_success(tmp_path: Path) -> None:
    connection, dispatcher, worker, _, _, image_provider = setup(tmp_path)
    dispatcher.dispatch(
        TelegramTextInput(
            update_id=3,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=3,
            text="Synthetic thought",
        ),
        now=NOW,
        billing_month="2026-10",
    )
    run(worker)
    dispatcher.dispatch(
        TelegramCallbackInput(
            update_id=4,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=4,
            query_id="query-4",
            data="txt_ok:tg-3:1",
        ),
        now=NOW,
        billing_month="2026-10",
    )

    result = run(worker)

    assert result.outcome is WorkerOutcome.IMAGE_STORED
    assert image_provider.calls == 1
    assert DraftRepository(connection).get("tg-3").state is DraftState.IMAGE_REVIEW
    image = connection.execute(
        "SELECT local_path, sha256 FROM image_versions WHERE draft_id = 'tg-3'"
    ).fetchone()
    stored = tmp_path / "media" / image["local_path"]
    assert stored.read_bytes() == b"synthetic-png"
    assert len(image["sha256"]) == 64


def test_image_revision_uses_bound_original_and_creates_a_new_version(
    tmp_path: Path,
) -> None:
    connection, dispatcher, worker, _, _, image_provider = setup(tmp_path)
    dispatcher.dispatch(
        TelegramTextInput(
            update_id=30,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=30,
            text="Synthetic thought",
        ),
        now=NOW,
        billing_month="2026-10",
    )
    run(worker)
    dispatcher.dispatch(
        TelegramCallbackInput(
            update_id=31,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=31,
            query_id="query-31",
            data="txt_ok:tg-30:1",
        ),
        now=NOW,
        billing_month="2026-10",
    )
    run(worker)
    first_relative_path = connection.execute(
        "SELECT local_path FROM image_versions WHERE draft_id = 'tg-30' AND version = 1"
    ).fetchone()[0]
    first_path = tmp_path / "media" / first_relative_path

    requested = dispatcher.dispatch(
        TelegramCallbackInput(
            update_id=32,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=32,
            query_id="query-32",
            data="img_rev:tg-30:1",
        ),
        now=NOW,
        billing_month="2026-10",
    )
    queued = dispatcher.dispatch(
        TelegramTextInput(
            update_id=33,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=33,
            text="Use a darker background",
            reply_to_message_id=32,
        ),
        now=NOW,
        billing_month="2026-10",
    )
    revised = run(worker)

    assert requested.outcome is DispatchOutcome.IMAGE_REVISION_REQUESTED
    assert queued.outcome is DispatchOutcome.IMAGE_REVISION_JOB_QUEUED
    assert revised.outcome is WorkerOutcome.IMAGE_STORED
    assert image_provider.calls == 2
    request = image_provider.requests[1]
    assert request.source_image_path == first_path
    assert request.width == 1200
    assert request.height == 1200
    assert "Use a darker background" in request.prompt
    workflow = DraftRepository(connection).get("tg-30")
    assert workflow.state is DraftState.IMAGE_REVIEW
    assert workflow.current_image_version == 2
    second_relative_path = connection.execute(
        "SELECT local_path FROM image_versions WHERE draft_id = 'tg-30' AND version = 2"
    ).fetchone()[0]
    assert (tmp_path / "media" / second_relative_path).is_file()


def test_missing_voice_file_fails_without_calling_provider(tmp_path: Path) -> None:
    connection, dispatcher, worker, _, transcription_provider, _ = setup(tmp_path)
    dispatcher.dispatch(
        TelegramVoiceInput(
            update_id=5,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=5,
            file_id="private-file-id",
            file_unique_id="stable-file-id",
            duration_seconds=10,
            file_size=100,
            mime_type="audio/ogg",
        ),
        now=NOW,
        billing_month="2026-10",
    )

    result = run(worker)

    assert result.outcome is WorkerOutcome.FAILED_BEFORE_CALL
    assert transcription_provider.calls == 0
    job = connection.execute("SELECT state FROM jobs WHERE id = ?", (result.job_id,)).fetchone()
    assert job["state"] == "FAILED"
    assert connection.execute("SELECT COUNT(*) FROM api_usage").fetchone()[0] == 0


def test_provider_exception_stays_unknown_and_is_never_released_for_retry(
    tmp_path: Path,
) -> None:
    connection, dispatcher, worker, text_provider, _, _ = setup(tmp_path)
    dispatcher.dispatch(
        TelegramTextInput(
            update_id=6,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=6,
            text="Synthetic thought",
        ),
        now=NOW,
        billing_month="2026-10",
    )

    def fail(_request):
        raise RuntimeError("synthetic provider failure")

    text_provider.generate_text = fail  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="synthetic provider failure"):
        run(worker)

    job = connection.execute("SELECT state FROM jobs").fetchone()
    assert job["state"] == "RESULT_UNKNOWN"
    later = worker.run_once(
        worker_id="worker-2",
        now="2026-10-12T16:00:00Z",
        lease_expires_at="2026-10-12T16:05:00Z",
    )
    assert later.outcome is WorkerOutcome.IDLE
    assert recover_orphaned_text_calls(
        connection=connection,
        owner_id=OWNER_ID,
        now="2026-10-12T16:00:01Z",
    ) == 1
    recovered = connection.execute(
        """
        SELECT j.error_category, u.billing_status, o.kind
        FROM jobs AS j
        JOIN api_usage AS u ON u.job_id = j.id
        JOIN telegram_outbox AS o ON o.job_id = j.id
        """
    ).fetchone()
    assert tuple(recovered) == (
        "process_exit_after_call",
        "UNKNOWN_BILLED",
        "PROCESSING_ALERT",
    )


def test_sanitized_text_provider_failure_is_accounted_and_alerted(
    tmp_path: Path,
) -> None:
    connection, dispatcher, worker, text_provider, _, _ = setup(tmp_path)
    dispatcher.dispatch(
        TelegramTextInput(
            update_id=7,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=7,
            text="Synthetic thought",
        ),
        now=NOW,
        billing_month="2026-10",
    )

    def fail(_request):
        raise ProviderCallError(
            "sanitized incomplete response",
            category="output_limit",
            metering=ProviderMetering(
                provider_model="mock-text",
                price_version="2026-10-12",
                actual_cost_usd=Decimal("0.001"),
                input_units=100,
                output_units=1600,
            ),
        )

    text_provider.generate_text = fail  # type: ignore[method-assign]

    result = run(worker)

    assert result.outcome is WorkerOutcome.FAILED_AFTER_CALL
    job = connection.execute(
        "SELECT id, state, error_category, lease_owner FROM jobs"
    ).fetchone()
    assert tuple(job)[1:] == ("RESULT_UNKNOWN", "output_limit", None)
    usage = connection.execute(
        "SELECT billing_status, input_units, output_units FROM api_usage"
    ).fetchone()
    assert tuple(usage) == ("UNKNOWN_BILLED", 100, 1600)
    assert DraftRepository(connection).get("tg-7").state is DraftState.FAILED_RECOVERABLE
    alert = connection.execute(
        "SELECT kind, job_id, state FROM telegram_outbox"
    ).fetchone()
    assert tuple(alert) == ("PROCESSING_ALERT", job["id"], "PENDING")
    repository = DeliveryRepository(connection)
    lease = repository.lease_next(
        worker_id="delivery-worker",
        now=NOW,
        lease_expires_at="2026-10-12T15:05:00Z",
    )
    assert lease is not None
    rendered = repository.render(lease=lease, media_directory=tmp_path / "media")
    assert rendered.kind is DeliveryKind.PROCESSING_ALERT
    assert rendered.retry_callback == f"txt_retry:tg-7:{job['id']}"

    retried = dispatcher.dispatch(
        TelegramCallbackInput(
            update_id=8,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=8,
            query_id="query-8",
            data=f"txt_retry:tg-7:{job['id']}",
        ),
        now="2026-10-12T15:01:00Z",
        billing_month="2026-10",
    )

    assert retried.outcome is DispatchOutcome.TEXT_JOB_QUEUED
    assert DraftRepository(connection).get("tg-7").state is DraftState.TEXT_GENERATING
    jobs = connection.execute("SELECT state FROM jobs ORDER BY id").fetchall()
    assert [row["state"] for row in jobs] == ["RESULT_UNKNOWN", "QUEUED"]


def test_text_revision_uses_bound_comment_and_creates_one_new_version(
    tmp_path: Path,
) -> None:
    connection, dispatcher, worker, text_provider, _, _ = setup(tmp_path)
    dispatcher.dispatch(
        TelegramTextInput(
            update_id=40,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=40,
            text="Synthetic thought",
        ),
        now=NOW,
        billing_month="2026-10",
    )
    first = run(worker)
    requested = dispatcher.dispatch(
        TelegramCallbackInput(
            update_id=41,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=41,
            query_id="query-41",
            data="txt_rev:tg-40:1",
        ),
        now=NOW,
        billing_month="2026-10",
    )
    queued = dispatcher.dispatch(
        TelegramTextInput(
            update_id=42,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=42,
            text="Make it shorter",
            reply_to_message_id=41,
        ),
        now=NOW,
        billing_month="2026-10",
    )

    second = run(worker)

    assert first.outcome is WorkerOutcome.TEXT_STORED
    assert requested.outcome is DispatchOutcome.TEXT_REVISION_REQUESTED
    assert queued.outcome is DispatchOutcome.TEXT_REVISION_JOB_QUEUED
    assert second.outcome is WorkerOutcome.TEXT_STORED
    assert text_provider.calls == 2
    assert text_provider.requests[1].current_text == "Synthetic generated post"
    assert text_provider.requests[1].revision_comment == "Make it shorter"
    version = DraftRepository(connection).get_text_version(
        draft_id="tg-40",
        version=2,
    )
    assert version.revision_comment == "Make it shorter"
    assert connection.execute(
        "SELECT COUNT(*) FROM jobs WHERE kind = 'generate_text'"
    ).fetchone()[0] == 2
    assert connection.execute(
        "SELECT COUNT(*) FROM telegram_outbox WHERE kind = 'TEXT_REVIEW'"
    ).fetchone()[0] == 2


def test_voice_text_revision_transcribes_once_and_uses_the_exact_binding(
    tmp_path: Path,
) -> None:
    connection, dispatcher, worker, text_provider, transcription_provider, _ = setup(
        tmp_path
    )
    dispatcher.dispatch(
        TelegramTextInput(
            update_id=200,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=200,
            text="Synthetic thought",
        ),
        now=NOW,
        billing_month="2026-10",
    )
    run(worker)
    dispatcher.dispatch(
        TelegramCallbackInput(
            update_id=201,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=201,
            query_id="query-201",
            data="txt_rev:tg-200:1",
        ),
        now=NOW,
        billing_month="2026-10",
    )
    queued = dispatcher.dispatch(
        TelegramVoiceInput(
            update_id=202,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=202,
            file_id="private-file-id",
            file_unique_id="stable-text-revision-voice",
            duration_seconds=10,
            file_size=100,
            mime_type="audio/ogg",
            reply_to_message_id=201,
        ),
        now=NOW,
        billing_month="2026-10",
    )
    voice_path = staged_voice_path(
        media_directory=tmp_path / "media",
        draft_id="tg-202",
    )
    voice_path.parent.mkdir(parents=True)
    voice_path.write_bytes(b"synthetic-revision-audio")

    transcription = run(worker)
    revision = run(worker)

    assert queued.outcome is DispatchOutcome.REVISION_TRANSCRIPTION_JOB_QUEUED
    assert transcription.outcome is WorkerOutcome.REVISION_TRANSCRIPT_STORED
    assert revision.outcome is WorkerOutcome.TEXT_STORED
    assert transcription.next_job_id == revision.job_id
    assert transcription_provider.calls == 1
    assert text_provider.calls == 2
    assert text_provider.requests[1].revision_comment == "Synthetic voice transcript"
    assert connection.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 1
    recorded = connection.execute(
        "SELECT source_kind, comment FROM text_revision_requests WHERE job_id = ?",
        (revision.job_id,),
    ).fetchone()
    assert tuple(recorded) == ("voice", "Synthetic voice transcript")


def test_missing_bound_revision_voice_cancels_only_the_dependent_generation(
    tmp_path: Path,
) -> None:
    connection, dispatcher, worker, _, transcription_provider, _ = setup(tmp_path)
    dispatcher.dispatch(
        TelegramTextInput(
            update_id=205,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=205,
            text="Synthetic thought",
        ),
        now=NOW,
        billing_month="2026-10",
    )
    run(worker)
    dispatcher.dispatch(
        TelegramCallbackInput(
            update_id=206,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=206,
            query_id="query-206",
            data="txt_rev:tg-205:1",
        ),
        now=NOW,
        billing_month="2026-10",
    )
    dispatcher.dispatch(
        TelegramVoiceInput(
            update_id=207,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=207,
            file_id="private-file-id",
            file_unique_id="missing-revision-voice",
            duration_seconds=10,
            file_size=100,
            mime_type="audio/ogg",
            reply_to_message_id=206,
        ),
        now=NOW,
        billing_month="2026-10",
    )

    result = run(worker)

    assert result.outcome is WorkerOutcome.FAILED_BEFORE_CALL
    assert transcription_provider.calls == 0
    states = connection.execute(
        """
        SELECT kind, state FROM jobs
        WHERE kind IN ('transcribe_revision', 'generate_text')
        ORDER BY id DESC LIMIT 2
        """
    ).fetchall()
    assert {(row["kind"], row["state"]) for row in states} == {
        ("transcribe_revision", "FAILED"),
        ("generate_text", "CANCELLED"),
    }
    assert connection.execute("SELECT COUNT(*) FROM pending_text_inputs").fetchone()[0] == 1


def test_voice_image_revision_transcribes_once_and_reuses_bound_original(
    tmp_path: Path,
) -> None:
    connection, dispatcher, worker, _, transcription_provider, image_provider = setup(
        tmp_path
    )
    dispatcher.dispatch(
        TelegramTextInput(
            update_id=210,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=210,
            text="Synthetic thought",
        ),
        now=NOW,
        billing_month="2026-10",
    )
    run(worker)
    dispatcher.dispatch(
        TelegramCallbackInput(
            update_id=211,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=211,
            query_id="query-211",
            data="txt_ok:tg-210:1",
        ),
        now=NOW,
        billing_month="2026-10",
    )
    run(worker)
    dispatcher.dispatch(
        TelegramCallbackInput(
            update_id=212,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=212,
            query_id="query-212",
            data="img_rev:tg-210:1",
        ),
        now=NOW,
        billing_month="2026-10",
    )
    queued = dispatcher.dispatch(
        TelegramVoiceInput(
            update_id=213,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=213,
            file_id="private-file-id",
            file_unique_id="stable-image-revision-voice",
            duration_seconds=8,
            file_size=80,
            mime_type="audio/ogg",
            reply_to_message_id=212,
        ),
        now=NOW,
        billing_month="2026-10",
    )
    voice_path = staged_voice_path(
        media_directory=tmp_path / "media",
        draft_id="tg-213",
    )
    voice_path.parent.mkdir(parents=True)
    voice_path.write_bytes(b"synthetic-image-revision-audio")

    transcription = run(worker)
    revision = run(worker)

    assert queued.outcome is DispatchOutcome.REVISION_TRANSCRIPTION_JOB_QUEUED
    assert transcription.outcome is WorkerOutcome.REVISION_TRANSCRIPT_STORED
    assert revision.outcome is WorkerOutcome.IMAGE_STORED
    assert transcription_provider.calls == 1
    assert image_provider.calls == 2
    assert image_provider.requests[1].source_image_path is not None
    assert "Synthetic voice transcript" in image_provider.requests[1].prompt
    workflow = DraftRepository(connection).get("tg-210")
    assert workflow.current_image_version == 2
    assert connection.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 1
