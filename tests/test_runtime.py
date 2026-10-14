from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from praeco.budget import BudgetPolicy
from praeco.dispatcher import DispatchCostEstimates, DispatchOutcome, TelegramDispatcher
from praeco.outbox import (
    DeliveryOutcome,
    DeliveryRunResult,
    DeliveryWorker,
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
from praeco.runtime import PraecoRuntime, RuntimeConfig
from praeco.storage import apply_migrations, connect_database
from praeco.telegram_api import (
    BotApiTelegramSender,
    PollingResult,
    RussianStatusResponder,
    TelegramBotApiClient,
    TelegramPollingAdapter,
)
from praeco.worker import SingleWorker, WorkerOutcome, WorkerResult

MIGRATIONS = Path(__file__).parents[1] / "migrations"
OWNER_ID = 101
FIXED_NOW = datetime(2026, 10, 12, 16, 0, tzinfo=UTC)


class FakeBotApiTransport:
    def __init__(self, updates: list[object]) -> None:
        self.updates = updates
        self.json_calls: list[tuple[str, dict[str, object]]] = []
        self.next_message_id = 70

    def call_json(self, *, method: str, payload):
        self.json_calls.append((method, dict(payload)))
        if method == "getUpdates":
            return {"ok": True, "result": self.updates}
        if method == "sendMessage":
            self.next_message_id += 1
            return {"ok": True, "result": {"message_id": self.next_message_id}}
        raise AssertionError(f"unexpected method: {method}")

    def call_multipart(self, **_kwargs):
        raise AssertionError("no image delivery expected")

    def download(self, *, file_path: str, max_bytes: int) -> bytes:
        raise AssertionError(f"no download expected: {file_path}, {max_bytes}")


def policy() -> BudgetPolicy:
    return BudgetPolicy(
        per_post_warning_usd=Decimal("0.50"),
        per_post_hard_limit_usd=Decimal("0.75"),
        monthly_hard_limit_usd=Decimal("25"),
    )


def text_update(update_id: int) -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {"id": OWNER_ID},
            "chat": {"id": OWNER_ID, "type": "private"},
            "text": "Synthetic private thought",
        },
    }


def test_runtime_wires_text_update_through_mock_generation_and_delivery(
    tmp_path: Path,
) -> None:
    connection = connect_database(tmp_path / "praeco.sqlite3")
    apply_migrations(connection, MIGRATIONS)
    media_directory = tmp_path / "media"
    transport = FakeBotApiTransport([text_update(41)])
    client = TelegramBotApiClient(transport)
    budget_policy = policy()
    text_provider = MockTextProvider(
        TextGenerationResult(
            body="Synthetic generated post",
            language="ru",
            image_brief="A synthetic scene",
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
        budget_policy=budget_policy,
        cost_estimates=DispatchCostEstimates(
            text_usd=Decimal("0.02"),
            transcription_usd=Decimal("0.01"),
            image_usd=Decimal("0.05"),
        ),
    )
    poller = TelegramPollingAdapter(
        client=client,
        dispatcher=dispatcher,
        owner_id=OWNER_ID,
        media_directory=media_directory,
        status_responder=RussianStatusResponder(client),
    )
    provider_worker = SingleWorker(
        connection=connection,
        owner_id=OWNER_ID,
        media_directory=media_directory,
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
        budget_policy=budget_policy,
        next_text_estimate_usd=Decimal("0.02"),
    )
    runtime = PraecoRuntime(
        poller=poller,
        provider_worker=provider_worker,
        delivery_worker=DeliveryWorker(
            connection=connection,
            media_directory=media_directory,
            sender=BotApiTelegramSender(client),
        ),
        clock=lambda: FIXED_NOW,
    )

    result = runtime.run_tick()

    assert runtime.next_offset == 42
    assert result.polling.outcomes == (DispatchOutcome.TEXT_JOB_QUEUED,)
    assert [item.outcome for item in result.provider_results] == [WorkerOutcome.TEXT_STORED]
    assert [item.outcome for item in result.delivery_results] == [DeliveryOutcome.SENT]
    assert text_provider.calls == 1
    sent_messages = [payload for method, payload in transport.json_calls if method == "sendMessage"]
    assert sent_messages[0]["text"] == "Мысль принята. Готовлю черновик текста."
    assert sent_messages[1]["text"] == "Synthetic generated post"
    assert "reply_markup" in sent_messages[1]
    assert connection.execute("SELECT state FROM jobs").fetchone()[0] == "SUCCEEDED"
    assert connection.execute("SELECT state FROM telegram_outbox").fetchone()[0] == "SENT"


class EmptyPoller:
    def poll_once(
        self,
        *,
        offset: int | None,
        timeout_seconds: int,
        now: str,
        billing_month: str,
    ) -> PollingResult:
        return PollingResult(
            next_offset=offset,
            received=0,
            dispatched=0,
            ignored=0,
        )


class BusyProviderWorker:
    def __init__(self) -> None:
        self.calls = 0

    def run_once(
        self,
        *,
        worker_id: str,
        now: str,
        lease_expires_at: str,
    ) -> WorkerResult:
        self.calls += 1
        return WorkerResult(outcome=WorkerOutcome.TEXT_STORED, job_id=self.calls)


class IdleDeliveryWorker:
    def run_once(
        self,
        *,
        worker_id: str,
        now: str,
        lease_expires_at: str,
        retry_at: str,
    ) -> DeliveryRunResult:
        return DeliveryRunResult(outcome=DeliveryOutcome.IDLE)


def test_runtime_caps_provider_work_per_tick() -> None:
    provider = BusyProviderWorker()
    runtime = PraecoRuntime(
        poller=EmptyPoller(),
        provider_worker=provider,
        delivery_worker=IdleDeliveryWorker(),
        config=RuntimeConfig(max_provider_jobs_per_tick=2),
        clock=lambda: FIXED_NOW,
    )

    result = runtime.run_tick()

    assert provider.calls == 2
    assert len(result.provider_results) == 2
    assert result.provider_limit_reached is True
    assert result.delivery_limit_reached is False


class StopAfterWait:
    def __init__(self) -> None:
        self.stopped = False

    def is_set(self) -> bool:
        return self.stopped


class IdleProviderWorker:
    def run_once(
        self,
        *,
        worker_id: str,
        now: str,
        lease_expires_at: str,
    ) -> WorkerResult:
        return WorkerResult(outcome=WorkerOutcome.IDLE)


def test_runtime_stops_cooperatively_after_an_idle_tick() -> None:
    stop = StopAfterWait()
    waits: list[float] = []

    def stop_during_wait(seconds: float) -> None:
        waits.append(seconds)
        stop.stopped = True

    runtime = PraecoRuntime(
        poller=EmptyPoller(),
        provider_worker=IdleProviderWorker(),
        delivery_worker=IdleDeliveryWorker(),
        config=RuntimeConfig(idle_sleep_seconds=0.25),
        clock=lambda: FIXED_NOW,
        idle_wait=stop_during_wait,
    )

    ticks = runtime.run_until_stopped(stop)

    assert ticks == 1
    assert waits == [0.25]
