from pathlib import Path

import pytest

from praeco.storage import apply_migrations, connect_database
from praeco.telegram import (
    MalformedTelegramUpdate,
    ProcessedUpdateRepository,
    TelegramCallbackInput,
    TelegramImageInput,
    TelegramTextInput,
    TelegramVoiceInput,
    UnauthorizedTelegramUpdate,
    parse_telegram_update,
)

MIGRATIONS = Path(__file__).parents[1] / "migrations"
OWNER_ID = 101


def message_update(*, text: str = "Одна мысль") -> dict[str, object]:
    return {
        "update_id": 500,
        "message": {
            "message_id": 10,
            "from": {"id": OWNER_ID},
            "chat": {"id": OWNER_ID, "type": "private"},
            "text": text,
        },
    }


def test_parses_private_owner_text_without_exposing_it_in_repr() -> None:
    event = parse_telegram_update(message_update(), owner_id=OWNER_ID)

    assert isinstance(event, TelegramTextInput)
    assert event.text == "Одна мысль"
    assert event.command is None
    assert "Одна мысль" not in repr(event)


@pytest.mark.parametrize(
    ("text", "command"),
    [("/start", "/start"), ("/NEW@Praeco_bot details", "/new")],
)
def test_normalizes_commands(text: str, command: str) -> None:
    event = parse_telegram_update(message_update(text=text), owner_id=OWNER_ID)

    assert isinstance(event, TelegramTextInput)
    assert event.command == command


def test_parses_reply_binding_for_text() -> None:
    payload = message_update(text="Сделай короче")
    message = payload["message"]
    assert isinstance(message, dict)
    message["reply_to_message"] = {"message_id": 9}

    event = parse_telegram_update(payload, owner_id=OWNER_ID)

    assert isinstance(event, TelegramTextInput)
    assert event.reply_to_message_id == 9


def test_parses_private_owner_voice_metadata() -> None:
    payload = message_update()
    message = payload["message"]
    assert isinstance(message, dict)
    message.pop("text")
    message["voice"] = {
        "file_id": "download-token",
        "file_unique_id": "stable-file-id",
        "duration": 42,
        "file_size": 12_345,
        "mime_type": "audio/ogg",
    }

    event = parse_telegram_update(payload, owner_id=OWNER_ID)

    assert isinstance(event, TelegramVoiceInput)
    assert event.duration_seconds == 42
    assert event.file_size == 12_345
    assert "download-token" not in repr(event)


def test_parses_largest_owner_photo_without_exposing_download_id() -> None:
    payload = message_update()
    message = payload["message"]
    assert isinstance(message, dict)
    message.pop("text")
    message["photo"] = [
        {
            "file_id": "small-download-id",
            "file_unique_id": "small-stable-id",
            "width": 90,
            "height": 90,
            "file_size": 100,
        },
        {
            "file_id": "large-download-id",
            "file_unique_id": "large-stable-id",
            "width": 1200,
            "height": 900,
            "file_size": 500,
        },
    ]

    event = parse_telegram_update(payload, owner_id=OWNER_ID)

    assert isinstance(event, TelegramImageInput)
    assert event.file_unique_id == "large-stable-id"
    assert (event.width, event.height) == (1200, 900)
    assert event.mime_type == "image/jpeg"
    assert "large-download-id" not in repr(event)


def test_parses_owner_document_for_later_content_validation() -> None:
    payload = message_update()
    message = payload["message"]
    assert isinstance(message, dict)
    message.pop("text")
    message["document"] = {
        "file_id": "document-download-id",
        "file_unique_id": "document-stable-id",
        "file_name": "original.png",
        "mime_type": "image/png",
        "file_size": 1234,
    }

    event = parse_telegram_update(payload, owner_id=OWNER_ID)

    assert isinstance(event, TelegramImageInput)
    assert event.file_size == 1234
    assert event.mime_type == "image/png"
    assert event.width is None
    assert "document-download-id" not in repr(event)


def test_parses_opaque_callback_within_telegram_limit() -> None:
    payload = {
        "update_id": 501,
        "callback_query": {
            "id": "query-secret",
            "from": {"id": OWNER_ID},
            "message": {
                "message_id": 11,
                "from": {"id": 999, "is_bot": True},
                "chat": {"id": OWNER_ID, "type": "private"},
            },
            "data": "approve_text:d1:v2",
        },
    }

    event = parse_telegram_update(payload, owner_id=OWNER_ID)

    assert isinstance(event, TelegramCallbackInput)
    assert event.data == "approve_text:d1:v2"
    assert "query-secret" not in repr(event)
    assert "approve_text" not in repr(event)


def test_rejects_non_owner_and_group_messages() -> None:
    non_owner = message_update()
    message = non_owner["message"]
    assert isinstance(message, dict)
    message["from"] = {"id": 202}
    message["chat"] = {"id": 202, "type": "private"}

    group = message_update()
    group_message = group["message"]
    assert isinstance(group_message, dict)
    group_message["chat"] = {"id": -1001, "type": "group"}

    with pytest.raises(UnauthorizedTelegramUpdate):
        parse_telegram_update(non_owner, owner_id=OWNER_ID)
    with pytest.raises(UnauthorizedTelegramUpdate):
        parse_telegram_update(group, owner_id=OWNER_ID)


def test_rejects_oversized_callback_data() -> None:
    payload = {
        "update_id": 501,
        "callback_query": {
            "id": "query",
            "from": {"id": OWNER_ID},
            "message": {
                "message_id": 11,
                "from": {"id": 999, "is_bot": True},
                "chat": {"id": OWNER_ID, "type": "private"},
            },
            "data": "я" * 33,
        },
    }

    with pytest.raises(MalformedTelegramUpdate, match="byte limit"):
        parse_telegram_update(payload, owner_id=OWNER_ID)


def test_rejects_callback_from_non_owner_even_in_owner_chat() -> None:
    payload = {
        "update_id": 501,
        "callback_query": {
            "id": "query",
            "from": {"id": 202},
            "message": {
                "message_id": 11,
                "from": {"id": 999, "is_bot": True},
                "chat": {"id": OWNER_ID, "type": "private"},
            },
            "data": "approve_text:d1:v2",
        },
    }

    with pytest.raises(UnauthorizedTelegramUpdate, match="callback sender"):
        parse_telegram_update(payload, owner_id=OWNER_ID)


def test_ignores_unsupported_well_formed_update() -> None:
    assert parse_telegram_update(
        {"update_id": 502, "my_chat_member": {}}, owner_id=OWNER_ID
    ) is None


def test_durable_claim_accepts_an_update_only_once(tmp_path: Path) -> None:
    database_path = tmp_path / "praeco.sqlite3"
    first_connection = connect_database(database_path)
    apply_migrations(first_connection, MIGRATIONS)
    first = ProcessedUpdateRepository(first_connection)

    assert first.claim_once(update_id=500, claimed_at="2026-10-12T12:00:00Z")
    assert not first.claim_once(update_id=500, claimed_at="2026-10-12T12:00:01Z")
    first_connection.close()

    second_connection = connect_database(database_path)
    second = ProcessedUpdateRepository(second_connection)
    assert not second.claim_once(update_id=500, claimed_at="2026-10-12T13:00:00Z")
    assert second.claim_once(update_id=501, claimed_at="2026-10-12T13:00:01Z")
    second_connection.close()
