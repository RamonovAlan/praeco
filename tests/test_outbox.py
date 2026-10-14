from decimal import Decimal
from pathlib import Path

import pytest

from praeco.budget import BudgetPolicy
from praeco.dispatcher import DispatchCostEstimates, DispatchOutcome, TelegramDispatcher
from praeco.outbox import (
    DeliveryError,
    DeliveryKind,
    DeliveryOutcome,
    DeliveryRepository,
    DeliveryRequest,
    DeliveryWorker,
    MockTelegramSender,
)
from praeco.providers import (
    ImageGenerationResult,
    MockImageProvider,
    MockTextProvider,
    MockTranscriptionProvider,
    ProviderMetering,
    TextGenerationResult,
    TranscriptionResult,
)
from praeco.storage import apply_migrations, connect_database
from praeco.telegram import TelegramCallbackInput, TelegramTextInput
from praeco.worker import SingleWorker

MIGRATIONS = Path(__file__).parents[1] / "migrations"
OWNER_ID = 101
NOW = "2026-10-12T15:00:00Z"
LATER = "2026-10-12T15:10:00Z"


class FailingSender:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, _delivery):
        self.calls += 1
        raise RuntimeError("synthetic Telegram failure")


def setup(tmp_path: Path):
    connection = connect_database(tmp_path / "praeco.sqlite3")
    apply_migrations(connection, MIGRATIONS)
    policy = BudgetPolicy(
        per_post_warning_usd=Decimal("0.50"),
        per_post_hard_limit_usd=Decimal("0.75"),
        monthly_hard_limit_usd=Decimal("25"),
    )
    text_provider = MockTextProvider(
        TextGenerationResult(
            body="Synthetic generated post",
            language="ru",
            image_brief="A synthetic dimensional scene",
            metering=ProviderMetering(
                provider_model="mock-text",
                price_version="2026-10-12",
                actual_cost_usd=Decimal("0.01"),
            ),
        )
    )
    dispatcher = TelegramDispatcher(
        connection=connection,
        owner_id=OWNER_ID,
        budget_policy=policy,
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
        transcription_provider=MockTranscriptionProvider(
            TranscriptionResult(
                transcript="Synthetic transcript",
                language="ru",
                metering=ProviderMetering(
                    provider_model="mock-transcription",
                    price_version="2026-10-12",
                    actual_cost_usd=Decimal("0.001"),
                ),
            )
        ),
        image_provider=MockImageProvider(
            ImageGenerationResult(
                content=b"synthetic-png",
                mime_type="image/png",
                width=1200,
                height=1200,
                metering=ProviderMetering(
                    provider_model="mock-image",
                    price_version="2026-10-12",
                    actual_cost_usd=Decimal("0.04"),
                ),
            )
        ),
        budget_policy=policy,
        next_text_estimate_usd=Decimal("0.02"),
    )
    return connection, dispatcher, worker, text_provider


def run_generation(worker: SingleWorker):
    return worker.run_once(
        worker_id="provider-worker",
        now=NOW,
        lease_expires_at="2026-10-12T15:05:00Z",
    )


def test_text_result_and_delivery_are_committed_together(tmp_path: Path) -> None:
    connection, dispatcher, worker, _ = setup(tmp_path)
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

    result = run_generation(worker)

    row = connection.execute(
        """
        SELECT kind, state, draft_id, chat_id, text_version, image_version
        FROM telegram_outbox
        """
    ).fetchone()
    assert result.draft_id == "tg-1"
    assert tuple(row) == ("TEXT_REVIEW", "PENDING", "tg-1", OWNER_ID, 1, None)


def test_failed_send_retries_delivery_without_repeating_generation(tmp_path: Path) -> None:
    connection, dispatcher, worker, text_provider = setup(tmp_path)
    dispatcher.dispatch(
        TelegramTextInput(
            update_id=2,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=2,
            text="Synthetic thought",
        ),
        now=NOW,
        billing_month="2026-10",
    )
    generation = run_generation(worker)
    failing_sender = FailingSender()
    delivery_worker = DeliveryWorker(
        connection=connection,
        media_directory=tmp_path / "media",
        sender=failing_sender,
    )

    first = delivery_worker.run_once(
        worker_id="delivery-worker-1",
        now=NOW,
        lease_expires_at="2026-10-12T15:05:00Z",
        retry_at=LATER,
    )

    assert first.outcome is DeliveryOutcome.RETRY_SCHEDULED
    assert text_provider.calls == 1
    assert connection.execute(
        "SELECT state FROM jobs WHERE id = ?", (generation.job_id,)
    ).fetchone()[0] == "SUCCEEDED"
    retry_row = connection.execute(
        "SELECT state, attempts FROM telegram_outbox"
    ).fetchone()
    assert tuple(retry_row) == ("PENDING", 1)

    sender = MockTelegramSender(message_id=77)
    restarted_worker = DeliveryWorker(
        connection=connection,
        media_directory=tmp_path / "media",
        sender=sender,
    )
    second = restarted_worker.run_once(
        worker_id="delivery-worker-2",
        now=LATER,
        lease_expires_at="2026-10-12T15:15:00Z",
        retry_at="2026-10-12T15:20:00Z",
    )

    assert second.outcome is DeliveryOutcome.SENT
    assert text_provider.calls == 1
    assert sender.calls == 1
    assert sender.deliveries[0].body == "Synthetic generated post"
    assert sender.deliveries[0].approve_callback == "txt_ok:tg-2:1"
    assert sender.deliveries[0].revision_callback == "txt_rev:tg-2:1"
    assert sender.deliveries[0].manual_edit_callback == "txt_edit:tg-2:1"
    sent_row = connection.execute(
        "SELECT state, attempts, telegram_message_id FROM telegram_outbox"
    ).fetchone()
    assert tuple(sent_row) == ("SENT", 2, 77)


def test_image_delivery_uses_exact_persisted_file(tmp_path: Path) -> None:
    connection, dispatcher, worker, _ = setup(tmp_path)
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
    run_generation(worker)
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
    run_generation(worker)
    sender = MockTelegramSender(message_id=88)
    delivery_worker = DeliveryWorker(
        connection=connection,
        media_directory=tmp_path / "media",
        sender=sender,
    )

    text_delivery = delivery_worker.run_once(
        worker_id="delivery-worker",
        now=NOW,
        lease_expires_at="2026-10-12T15:05:00Z",
        retry_at=LATER,
    )
    image_delivery = delivery_worker.run_once(
        worker_id="delivery-worker",
        now=NOW,
        lease_expires_at="2026-10-12T15:05:00Z",
        retry_at=LATER,
    )

    assert text_delivery.outcome is DeliveryOutcome.SENT
    assert image_delivery.outcome is DeliveryOutcome.SENT
    rendered = sender.deliveries[1]
    assert rendered.kind is DeliveryKind.IMAGE_REVIEW
    assert rendered.image_path is not None
    assert rendered.image_path.read_bytes() == b"synthetic-png"
    assert rendered.approve_callback == "img_ok:tg-3:1"


def test_image_approval_enqueues_final_snapshot_with_measured_cost(
    tmp_path: Path,
) -> None:
    connection, dispatcher, worker, _ = setup(tmp_path)
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
    run_generation(worker)
    dispatcher.dispatch(
        TelegramCallbackInput(
            update_id=41,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=41,
            query_id="query-41",
            data="txt_ok:tg-40:1",
        ),
        now=NOW,
        billing_month="2026-10",
    )
    run_generation(worker)

    approved = dispatcher.dispatch(
        TelegramCallbackInput(
            update_id=42,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=42,
            query_id="query-42",
            data="img_ok:tg-40:1",
        ),
        now=NOW,
        billing_month="2026-10",
    )

    assert approved.outcome is DispatchOutcome.IMAGE_APPROVED
    final_row = connection.execute(
        """
        SELECT kind, text_version, image_version, state
        FROM telegram_outbox WHERE kind = 'FINAL_REVIEW'
        """
    ).fetchone()
    assert tuple(final_row) == ("FINAL_REVIEW", 1, 1, "PENDING")
    repository = DeliveryRepository(connection)
    lease = repository.lease_next(
        worker_id="delivery-worker",
        now=NOW,
        lease_expires_at="2026-10-12T15:05:00Z",
    )
    while lease is not None and lease.kind is not DeliveryKind.FINAL_REVIEW:
        repository.mark_sent(
            delivery_id=lease.delivery_id,
            worker_id="delivery-worker",
            telegram_message_id=100 + lease.delivery_id,
            now=NOW,
        )
        lease = repository.lease_next(
            worker_id="delivery-worker",
            now=NOW,
            lease_expires_at="2026-10-12T15:05:00Z",
        )
    assert lease is not None
    rendered = repository.render(lease=lease, media_directory=tmp_path / "media")
    assert rendered.body == "Synthetic generated post"
    assert rendered.image_path is not None
    assert rendered.image_path.read_bytes() == b"synthetic-png"
    assert rendered.text_version == 1
    assert rendered.image_version == 1
    assert rendered.cost_usd == Decimal("0.05")

    enabled_repository = DeliveryRepository(connection, publishing_enabled=True)
    rendered_with_publish = enabled_repository.render(
        lease=lease,
        media_directory=tmp_path / "media",
    )
    assert rendered_with_publish.publish_callback == "pub:tg-40:1:1"


def test_enqueue_is_idempotent_and_rejects_key_reuse(tmp_path: Path) -> None:
    connection, dispatcher, worker, _ = setup(tmp_path)
    dispatcher.dispatch(
        TelegramTextInput(
            update_id=5,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=5,
            text="Synthetic thought",
        ),
        now=NOW,
        billing_month="2026-10",
    )
    run_generation(worker)
    repository = DeliveryRepository(connection)
    request = DeliveryRequest(
        kind=DeliveryKind.TEXT_REVIEW,
        draft_id="tg-5",
        chat_id=OWNER_ID,
        text_version=1,
        deduplication_key="telegram:text-review:tg-5:1",
        now=NOW,
    )

    first = repository.enqueue(request)
    second = repository.enqueue(request)

    assert first == second
    assert connection.execute("SELECT COUNT(*) FROM telegram_outbox").fetchone()[0] == 1
    with pytest.raises(DeliveryError, match="another delivery"):
        repository.enqueue(
            DeliveryRequest(
                kind=DeliveryKind.TEXT_REVIEW,
                draft_id="tg-5",
                chat_id=OWNER_ID + 1,
                text_version=1,
                deduplication_key=request.deduplication_key,
                now=NOW,
            )
        )


def test_wrong_worker_cannot_complete_a_delivery(tmp_path: Path) -> None:
    connection, dispatcher, worker, _ = setup(tmp_path)
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
    run_generation(worker)
    repository = DeliveryRepository(connection)
    lease = repository.lease_next(
        worker_id="owner-worker",
        now=NOW,
        lease_expires_at="2026-10-12T15:05:00Z",
    )
    assert lease is not None

    with pytest.raises(DeliveryError, match="not leased"):
        repository.mark_sent(
            delivery_id=lease.delivery_id,
            worker_id="forged-worker",
            telegram_message_id=1,
            now=NOW,
        )


def test_delivery_stops_after_bounded_attempts(tmp_path: Path) -> None:
    connection, dispatcher, worker, _ = setup(tmp_path)
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
    run_generation(worker)
    delivery_worker = DeliveryWorker(
        connection=connection,
        media_directory=tmp_path / "media",
        sender=FailingSender(),
        max_attempts=1,
    )

    result = delivery_worker.run_once(
        worker_id="delivery-worker",
        now=NOW,
        lease_expires_at="2026-10-12T15:05:00Z",
        retry_at=LATER,
    )

    assert result.outcome is DeliveryOutcome.FAILED
    failed_row = connection.execute(
        "SELECT state, error_category FROM telegram_outbox"
    ).fetchone()
    assert tuple(failed_row) == ("FAILED", "telegram_send_failed")


def test_image_delivery_rejects_a_path_outside_private_media(tmp_path: Path) -> None:
    connection, dispatcher, worker, _ = setup(tmp_path)
    dispatcher.dispatch(
        TelegramTextInput(
            update_id=8,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=8,
            text="Synthetic thought",
        ),
        now=NOW,
        billing_month="2026-10",
    )
    run_generation(worker)
    dispatcher.dispatch(
        TelegramCallbackInput(
            update_id=9,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=9,
            query_id="query-9",
            data="txt_ok:tg-8:1",
        ),
        now=NOW,
        billing_month="2026-10",
    )
    run_generation(worker)
    connection.execute(
        "UPDATE image_versions SET local_path = '../../outside.png' WHERE draft_id = 'tg-8'"
    )
    connection.commit()
    text_sender = MockTelegramSender(message_id=90)
    delivery_worker = DeliveryWorker(
        connection=connection,
        media_directory=tmp_path / "media",
        sender=text_sender,
    )
    delivery_worker.run_once(
        worker_id="delivery-worker",
        now=NOW,
        lease_expires_at="2026-10-12T15:05:00Z",
        retry_at=LATER,
    )

    result = delivery_worker.run_once(
        worker_id="delivery-worker",
        now=NOW,
        lease_expires_at="2026-10-12T15:05:00Z",
        retry_at=LATER,
    )

    assert result.outcome is DeliveryOutcome.FAILED
    assert text_sender.calls == 1
    image_row = connection.execute(
        """
        SELECT state, error_category FROM telegram_outbox
        WHERE kind = 'IMAGE_REVIEW'
        """
    ).fetchone()
    assert tuple(image_row) == ("FAILED", "invalid_persisted_payload")
