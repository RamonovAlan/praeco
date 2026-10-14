import json
from decimal import Decimal
from pathlib import Path

import pytest

from praeco.budget import BudgetPolicy
from praeco.dispatcher import (
    DispatchCostEstimates,
    DispatchOutcome,
    DispatchResult,
    TelegramDispatcher,
)
from praeco.drafts import DraftRepository
from praeco.outbox import DeliveryKind, TelegramDelivery
from praeco.storage import apply_migrations, connect_database
from praeco.telegram import TelegramCallbackInput, TelegramTextInput, parse_telegram_update
from praeco.telegram_api import (
    BotApiTelegramSender,
    MediaRejection,
    RussianStatusResponder,
    TelegramApiError,
    TelegramBotApiClient,
    TelegramPayloadError,
    TelegramPollingAdapter,
    UrllibTelegramTransport,
)
from praeco.worker import staged_voice_path

MIGRATIONS = Path(__file__).parents[1] / "migrations"
OWNER_ID = 101
NOW = "2026-10-12T16:00:00Z"


class FakeTransport:
    def __init__(self) -> None:
        self.json_responses: dict[str, object] = {}
        self.json_calls: list[tuple[str, dict[str, object]]] = []
        self.multipart_calls: list[dict[str, object]] = []
        self.download_calls: list[tuple[str, int]] = []
        self.download_content = b"synthetic-audio"

    def call_json(self, *, method: str, payload):
        self.json_calls.append((method, dict(payload)))
        return self.json_responses[method]

    def call_multipart(
        self,
        *,
        method: str,
        fields,
        file_field: str,
        file_path: Path,
        mime_type: str,
        max_bytes: int,
    ):
        self.multipart_calls.append(
            {
                "method": method,
                "fields": dict(fields),
                "file_field": file_field,
                "file_path": file_path,
                "mime_type": mime_type,
                "max_bytes": max_bytes,
            }
        )
        return {"ok": True, "result": {"message_id": 88}}

    def download(self, *, file_path: str, max_bytes: int) -> bytes:
        self.download_calls.append((file_path, max_bytes))
        return self.download_content


class FakePollingClient:
    def __init__(
        self,
        updates: tuple[object, ...],
        *,
        content: bytes = b"voice",
        expected_file_id: str = "private-file-id",
        expected_max_bytes: int = 20_000_000,
    ) -> None:
        self.updates = updates
        self.content = content
        self.expected_file_id = expected_file_id
        self.expected_max_bytes = expected_max_bytes
        self.get_updates_calls: list[tuple[int | None, int]] = []
        self.downloads = 0
        self.answered: list[str] = []

    def get_updates(
        self,
        *,
        offset: int | None,
        timeout_seconds: int,
        limit: int = 20,
    ) -> tuple[object, ...]:
        self.get_updates_calls.append((offset, timeout_seconds))
        return self.updates

    def download_file(self, *, file_id: str, max_bytes: int) -> bytes:
        assert file_id == self.expected_file_id
        assert max_bytes == self.expected_max_bytes
        self.downloads += 1
        return self.content

    def answer_callback_query(self, *, query_id: str) -> None:
        self.answered.append(query_id)


class FailingStatusResponder:
    def respond(self, _event, _result) -> None:
        raise TelegramApiError("synthetic status failure")

    def reject_media(self, _event, _reason) -> None:
        raise TelegramApiError("synthetic status failure")


class RecordingStatusResponder:
    def __init__(self) -> None:
        self.rejections: list[MediaRejection] = []

    def respond(self, _event, _result) -> None:
        return None

    def reject_media(self, _event, reason: MediaRejection) -> None:
        self.rejections.append(reason)


def dispatcher(tmp_path: Path):
    connection = connect_database(tmp_path / "praeco.sqlite3")
    apply_migrations(connection, MIGRATIONS)
    result = TelegramDispatcher(
        connection=connection,
        owner_id=OWNER_ID,
        budget_policy=BudgetPolicy(
            per_post_warning_usd=Decimal("0.50"),
            per_post_hard_limit_usd=Decimal("0.75"),
            monthly_hard_limit_usd=Decimal("25"),
        ),
        cost_estimates=DispatchCostEstimates(
            text_usd=Decimal("0.02"),
            transcription_usd=Decimal("0.01"),
            image_usd=Decimal("0.20"),
        ),
        media_directory=tmp_path / "media",
    )
    return connection, result


def text_update(update_id: int = 1) -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {"id": OWNER_ID},
            "chat": {"id": OWNER_ID, "type": "private"},
            "text": "Synthetic thought",
        },
    }


def voice_update(update_id: int = 2) -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {"id": OWNER_ID},
            "chat": {"id": OWNER_ID, "type": "private"},
            "voice": {
                "file_id": "private-file-id",
                "file_unique_id": "stable-file-id",
                "duration": 10,
                "file_size": 5,
                "mime_type": "audio/ogg",
            },
        },
    }


def image_document_update(
    update_id: int = 3,
    *,
    file_size: int = 100,
) -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {"id": OWNER_ID},
            "chat": {"id": OWNER_ID, "type": "private"},
            "reply_to_message": {"message_id": update_id - 1},
            "document": {
                "file_id": "owner-image-id",
                "file_unique_id": "owner-image-stable-id",
                "file_name": "original.png",
                "mime_type": "image/png",
                "file_size": file_size,
            },
        },
    }


def callback_update(update_id: int = 3) -> dict[str, object]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": "private-query-id",
            "from": {"id": OWNER_ID},
            "message": {
                "message_id": 5,
                "chat": {"id": OWNER_ID, "type": "private"},
            },
            "data": "unsupported",
        },
    }


def test_client_builds_bounded_long_poll_request() -> None:
    transport = FakeTransport()
    transport.json_responses["getUpdates"] = {
        "ok": True,
        "result": [text_update()],
    }
    client = TelegramBotApiClient(transport)

    updates = client.get_updates(offset=10, timeout_seconds=30)

    assert updates == (text_update(),)
    method, payload = transport.json_calls[0]
    assert method == "getUpdates"
    assert payload == {
        "offset": 10,
        "timeout": 30,
        "limit": 20,
        "allowed_updates": ["message", "callback_query"],
    }


def test_client_downloads_only_valid_bounded_file() -> None:
    transport = FakeTransport()
    transport.json_responses["getFile"] = {
        "ok": True,
        "result": {"file_path": "voice/file.oga", "file_size": 15},
    }
    client = TelegramBotApiClient(transport)

    content = client.download_file(file_id="private", max_bytes=20)

    assert content == b"synthetic-audio"
    assert transport.download_calls == [("voice/file.oga", 20)]

    transport.json_responses["getFile"] = {
        "ok": True,
        "result": {"file_path": "voice/file.oga", "file_size": 21},
    }
    with pytest.raises(TelegramApiError, match="size limit"):
        client.download_file(file_id="private", max_bytes=20)


def test_api_rejection_does_not_echo_remote_description() -> None:
    transport = FakeTransport()
    transport.json_responses["getUpdates"] = {
        "ok": False,
        "description": "private remote detail",
    }
    client = TelegramBotApiClient(transport)

    with pytest.raises(TelegramApiError) as captured:
        client.get_updates(offset=None, timeout_seconds=30)

    assert "private remote detail" not in str(captured.value)


def test_transport_repr_and_errors_do_not_expose_token() -> None:
    transport = UrllibTelegramTransport(token="synthetic-private-token")

    assert "synthetic-private-token" not in repr(transport)
    with pytest.raises(TelegramPayloadError):
        transport.download(file_path="../secret", max_bytes=10)


def test_text_sender_uses_russian_version_bound_keyboard() -> None:
    transport = FakeTransport()
    transport.json_responses["sendMessage"] = {
        "ok": True,
        "result": {"message_id": 77},
    }
    sender = BotApiTelegramSender(TelegramBotApiClient(transport))
    delivery = TelegramDelivery(
        delivery_id=1,
        kind=DeliveryKind.TEXT_REVIEW,
        chat_id=OWNER_ID,
        draft_id="tg-1",
        approve_callback="txt_ok:tg-1:2",
        revision_callback="txt_rev:tg-1:2",
        manual_edit_callback="txt_edit:tg-1:2",
        cancel_callback="txt_cancel:tg-1:2",
        body="Synthetic post",
    )

    message_id = sender.send(delivery)

    assert message_id == 77
    method, payload = transport.json_calls[0]
    assert method == "sendMessage"
    assert payload["text"] == "Synthetic post"
    keyboard = payload["reply_markup"]
    assert isinstance(keyboard, dict)
    assert keyboard["inline_keyboard"] == [
        [{"text": "Утвердить текст", "callback_data": "txt_ok:tg-1:2"}],
        [{"text": "Дать правки", "callback_data": "txt_rev:tg-1:2"}],
        [{"text": "Изменить вручную", "callback_data": "txt_edit:tg-1:2"}],
        [{"text": "Отменить", "callback_data": "txt_cancel:tg-1:2"}],
    ]


def test_image_sender_uploads_preview_without_changing_original(tmp_path: Path) -> None:
    image = tmp_path / "preview.png"
    image.write_bytes(b"synthetic-png")
    transport = FakeTransport()
    sender = BotApiTelegramSender(TelegramBotApiClient(transport))
    delivery = TelegramDelivery(
        delivery_id=2,
        kind=DeliveryKind.IMAGE_REVIEW,
        chat_id=OWNER_ID,
        draft_id="tg-2",
        approve_callback="img_ok:tg-2:1",
        revision_callback="img_rev:tg-2:1",
        owner_upload_callback="img_upload:tg-2:1",
        return_to_text_callback="img_text:tg-2:1",
        cancel_callback="img_cancel:tg-2:1",
        image_path=image,
    )

    message_id = sender.send(delivery)

    assert message_id == 88
    call = transport.multipart_calls[0]
    assert call["method"] == "sendPhoto"
    assert call["file_path"] == image
    assert image.read_bytes() == b"synthetic-png"
    markup = json.loads(call["fields"]["reply_markup"])
    assert markup["inline_keyboard"] == [
        [{"text": "Утвердить изображение", "callback_data": "img_ok:tg-2:1"}],
        [{"text": "Дать правки", "callback_data": "img_rev:tg-2:1"}],
        [{"text": "Загрузить своё", "callback_data": "img_upload:tg-2:1"}],
        [{"text": "Вернуться к тексту", "callback_data": "img_text:tg-2:1"}],
        [{"text": "Отменить", "callback_data": "img_cancel:tg-2:1"}],
    ]


def test_final_sender_uploads_approved_image_with_text_and_cost(tmp_path: Path) -> None:
    image = tmp_path / "final.png"
    image.write_bytes(b"synthetic-png")
    transport = FakeTransport()
    sender = BotApiTelegramSender(TelegramBotApiClient(transport))
    delivery = TelegramDelivery(
        delivery_id=3,
        kind=DeliveryKind.FINAL_REVIEW,
        chat_id=OWNER_ID,
        draft_id="tg-3",
        body="Synthetic final post",
        image_path=image,
        text_version=1,
        image_version=2,
        cost_usd=Decimal("0.051"),
    )

    message_id = sender.send(delivery)

    assert message_id == 88
    call = transport.multipart_calls[0]
    assert call["method"] == "sendPhoto"
    assert call["file_path"] == image
    assert "Synthetic final post" in call["fields"]["caption"]
    assert "$0.051000" in call["fields"]["caption"]
    assert "reply_markup" not in call["fields"]


def test_final_sender_exposes_publish_button_only_when_bound(tmp_path: Path) -> None:
    image = tmp_path / "final.png"
    image.write_bytes(b"synthetic-png")
    transport = FakeTransport()
    sender = BotApiTelegramSender(TelegramBotApiClient(transport))
    delivery = TelegramDelivery(
        delivery_id=31,
        kind=DeliveryKind.FINAL_REVIEW,
        chat_id=OWNER_ID,
        draft_id="tg-31",
        body="Synthetic final post",
        image_path=image,
        text_version=2,
        image_version=3,
        cost_usd=Decimal("0.05"),
        publish_callback="pub:tg-31:2:3",
    )

    sender.send(delivery)

    call = transport.multipart_calls[0]
    markup = json.loads(call["fields"]["reply_markup"])
    assert markup["inline_keyboard"] == [
        [
            {
                "text": "Опубликовать в LinkedIn",
                "callback_data": "pub:tg-31:2:3",
            }
        ]
    ]


def test_processing_alert_sender_exposes_explicit_retry_button() -> None:
    transport = FakeTransport()
    transport.json_responses["sendMessage"] = {
        "ok": True,
        "result": {"message_id": 77},
    }
    sender = BotApiTelegramSender(TelegramBotApiClient(transport))
    delivery = TelegramDelivery(
        delivery_id=4,
        kind=DeliveryKind.PROCESSING_ALERT,
        chat_id=OWNER_ID,
        draft_id="tg-4",
        body="Synthetic safe failure explanation",
        retry_callback="txt_retry:tg-4:9",
    )

    message_id = sender.send(delivery)

    assert message_id == 77
    method, payload = transport.json_calls[0]
    assert method == "sendMessage"
    assert payload["text"] == "Synthetic safe failure explanation"
    assert payload["reply_markup"] == {
        "inline_keyboard": [
            [
                {
                    "text": "Повторить создание текста",
                    "callback_data": "txt_retry:tg-4:9",
                }
            ]
        ]
    }


def test_russian_status_responder_reports_safe_runtime_mode() -> None:
    transport = FakeTransport()
    transport.json_responses["sendMessage"] = {
        "ok": True,
        "result": {"message_id": 77},
    }
    responder = RussianStatusResponder(TelegramBotApiClient(transport))
    event = parse_text_event(text="/status")

    responder.respond(event, DispatchResult(outcome=DispatchOutcome.STATUS))
    responder.respond(event, DispatchResult(outcome=DispatchOutcome.DUPLICATE))

    assert len(transport.json_calls) == 1
    method, payload = transport.json_calls[0]
    assert method == "sendMessage"
    assert payload["chat_id"] == OWNER_ID
    assert payload["text"] == (
        "Praeco работает. Реальные AI-вызовы отключены, используются mocks. "
        "Публикация в LinkedIn отключена. LinkedIn OAuth не настроен."
    )


def test_polling_dispatches_text_and_advances_offset(tmp_path: Path) -> None:
    connection, inbound = dispatcher(tmp_path)
    client = FakePollingClient((text_update(10),))
    adapter = TelegramPollingAdapter(
        client=client,
        dispatcher=inbound,
        owner_id=OWNER_ID,
        media_directory=tmp_path / "media",
    )

    result = adapter.poll_once(
        offset=None,
        timeout_seconds=30,
        now=NOW,
        billing_month="2026-10",
    )

    assert result.next_offset == 11
    assert result.outcomes == (DispatchOutcome.TEXT_JOB_QUEUED,)
    assert connection.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_polling_stages_voice_before_queueing_transcription(tmp_path: Path) -> None:
    connection, inbound = dispatcher(tmp_path)
    client = FakePollingClient((voice_update(20),), content=b"synthetic-voice")
    adapter = TelegramPollingAdapter(
        client=client,
        dispatcher=inbound,
        owner_id=OWNER_ID,
        media_directory=tmp_path / "media",
    )

    result = adapter.poll_once(
        offset=20,
        timeout_seconds=30,
        now=NOW,
        billing_month="2026-10",
    )

    assert result.next_offset == 21
    assert result.outcomes == (DispatchOutcome.TRANSCRIPTION_JOB_QUEUED,)
    assert client.downloads == 1
    voice_path = staged_voice_path(
        media_directory=tmp_path / "media",
        draft_id="tg-20",
    )
    assert voice_path.read_bytes() == b"synthetic-voice"
    assert voice_path.stat().st_mode & 0o777 == 0o600
    assert connection.execute(
        "SELECT state FROM jobs WHERE kind = 'transcribe_voice'"
    ).fetchone()[0] == "QUEUED"


def test_polling_keeps_staged_voice_for_a_bound_revision(tmp_path: Path) -> None:
    connection, inbound = dispatcher(tmp_path)
    prepare_text_revision_binding(connection, inbound, update_id=25)
    client = FakePollingClient((voice_update(27),), content=b"synthetic-revision-voice")
    adapter = TelegramPollingAdapter(
        client=client,
        dispatcher=inbound,
        owner_id=OWNER_ID,
        media_directory=tmp_path / "media",
    )

    result = adapter.poll_once(
        offset=27,
        timeout_seconds=30,
        now=NOW,
        billing_month="2026-10",
    )

    assert result.next_offset == 28
    assert result.outcomes == (DispatchOutcome.REVISION_TRANSCRIPTION_JOB_QUEUED,)
    voice_path = staged_voice_path(
        media_directory=tmp_path / "media",
        draft_id="tg-27",
    )
    assert voice_path.read_bytes() == b"synthetic-revision-voice"
    assert connection.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3


def test_polling_stores_bound_owner_image_without_an_ai_job(tmp_path: Path) -> None:
    connection, inbound = dispatcher(tmp_path)
    prepare_image_upload_binding(connection, inbound, update_id=60)
    content = synthetic_png()
    client = FakePollingClient(
        (image_document_update(62, file_size=len(content)),),
        content=content,
        expected_file_id="owner-image-id",
        expected_max_bytes=10_000_000,
    )
    adapter = TelegramPollingAdapter(
        client=client,
        dispatcher=inbound,
        owner_id=OWNER_ID,
        media_directory=tmp_path / "media",
    )

    result = adapter.poll_once(
        offset=62,
        timeout_seconds=30,
        now=NOW,
        billing_month="2026-10",
    )

    assert result.next_offset == 63
    assert result.outcomes == (DispatchOutcome.OWNER_IMAGE_STORED,)
    assert client.downloads == 1
    assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    image = connection.execute(
        "SELECT local_path, prompt FROM image_versions WHERE draft_id = 'tg-60' AND version = 2"
    ).fetchone()
    assert image["prompt"] is None
    assert (tmp_path / "media" / image["local_path"]).read_bytes() == content


def test_polling_explains_oversized_owner_image_and_advances_offset(
    tmp_path: Path,
) -> None:
    _, inbound = dispatcher(tmp_path)
    responder = RecordingStatusResponder()
    client = FakePollingClient((image_document_update(70, file_size=10_000_001),))
    adapter = TelegramPollingAdapter(
        client=client,
        dispatcher=inbound,
        owner_id=OWNER_ID,
        media_directory=tmp_path / "media",
        status_responder=responder,
    )

    result = adapter.poll_once(
        offset=70,
        timeout_seconds=30,
        now=NOW,
        billing_month="2026-10",
    )

    assert result.next_offset == 71
    assert result.ignored == 1
    assert result.dispatched == 0
    assert client.downloads == 0
    assert responder.rejections == [MediaRejection.TOO_LARGE]


def test_polling_ignores_unauthorized_update_without_leaking_or_blocking(
    tmp_path: Path,
) -> None:
    connection, inbound = dispatcher(tmp_path)
    payload = text_update(30)
    message = payload["message"]
    assert isinstance(message, dict)
    message["from"] = {"id": 999}
    message["chat"] = {"id": 999, "type": "private"}
    adapter = TelegramPollingAdapter(
        client=FakePollingClient((payload,)),
        dispatcher=inbound,
        owner_id=OWNER_ID,
        media_directory=tmp_path / "media",
    )

    result = adapter.poll_once(
        offset=30,
        timeout_seconds=30,
        now=NOW,
        billing_month="2026-10",
    )

    assert result.next_offset == 31
    assert result.ignored == 1
    assert result.dispatched == 0
    assert connection.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 0


def test_polling_answers_callback_even_when_domain_action_is_rejected(
    tmp_path: Path,
) -> None:
    _, inbound = dispatcher(tmp_path)
    client = FakePollingClient((callback_update(40),))
    adapter = TelegramPollingAdapter(
        client=client,
        dispatcher=inbound,
        owner_id=OWNER_ID,
        media_directory=tmp_path / "media",
    )

    result = adapter.poll_once(
        offset=40,
        timeout_seconds=30,
        now=NOW,
        billing_month="2026-10",
    )

    assert result.outcomes == (DispatchOutcome.REJECTED,)
    assert client.answered == ["private-query-id"]


def test_polling_status_failure_does_not_repeat_dispatched_work(tmp_path: Path) -> None:
    connection, inbound = dispatcher(tmp_path)
    adapter = TelegramPollingAdapter(
        client=FakePollingClient((text_update(50),)),
        dispatcher=inbound,
        owner_id=OWNER_ID,
        media_directory=tmp_path / "media",
        status_responder=FailingStatusResponder(),
    )

    result = adapter.poll_once(
        offset=50,
        timeout_seconds=30,
        now=NOW,
        billing_month="2026-10",
    )

    assert result.next_offset == 51
    assert result.response_failures == 1
    assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def parse_text_event(*, text: str):
    event = parse_telegram_update(
        {
            "update_id": 900,
            "message": {
                "message_id": 900,
                "from": {"id": OWNER_ID},
                "chat": {"id": OWNER_ID, "type": "private"},
                "text": text,
            },
        },
        owner_id=OWNER_ID,
    )
    assert event is not None
    return event


def prepare_image_upload_binding(
    connection,
    inbound: TelegramDispatcher,
    *,
    update_id: int,
) -> None:
    inbound.dispatch(
        TelegramTextInput(
            update_id=update_id,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=update_id,
            text="Synthetic thought",
        ),
        now=NOW,
        billing_month="2026-10",
    )
    drafts = DraftRepository(connection)
    drafts.record_text(
        draft_id=f"tg-{update_id}",
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
    drafts.approve_text(
        draft_id=f"tg-{update_id}",
        actor_id=OWNER_ID,
        version=1,
        now=NOW,
    )
    drafts.start_image_generation(
        draft_id=f"tg-{update_id}",
        actor_id=OWNER_ID,
        now=NOW,
    )
    drafts.record_image(
        draft_id=f"tg-{update_id}",
        actor_id=OWNER_ID,
        version=1,
        source_text_version=1,
        brief="Synthetic visual brief",
        prompt="Synthetic visual prompt",
        local_path=f"tg-{update_id}/image-v1.png",
        sha256="a" * 64,
        mime_type="image/png",
        width=1200,
        height=1200,
        now=NOW,
    )
    inbound.dispatch(
        TelegramCallbackInput(
            update_id=update_id + 1,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=update_id + 1,
            query_id=f"query-{update_id + 1}",
            data=f"img_upload:tg-{update_id}:1",
        ),
        now=NOW,
        billing_month="2026-10",
    )


def prepare_text_revision_binding(
    connection,
    inbound: TelegramDispatcher,
    *,
    update_id: int,
) -> None:
    inbound.dispatch(
        TelegramTextInput(
            update_id=update_id,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=update_id,
            text="Synthetic thought",
        ),
        now=NOW,
        billing_month="2026-10",
    )
    drafts = DraftRepository(connection)
    drafts.record_text(
        draft_id=f"tg-{update_id}",
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
    inbound.dispatch(
        TelegramCallbackInput(
            update_id=update_id + 1,
            actor_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=update_id + 1,
            query_id=f"query-{update_id + 1}",
            data=f"txt_rev:tg-{update_id}:1",
        ),
        now=NOW,
        billing_month="2026-10",
    )


def synthetic_png(*, width: int = 800, height: int = 600) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
        + b"\x00\x00\x00\x00"
    )
