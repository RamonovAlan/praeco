"""Safe Telegram update parsing and durable update deduplication."""

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from praeco.storage import immediate_transaction

_COMMAND_PATTERN = re.compile(r"^/[a-z0-9_]{1,32}$")
_MAX_CALLBACK_DATA_BYTES = 64


class TelegramUpdateError(ValueError):
    """Base class for redacted inbound-update validation failures."""


class MalformedTelegramUpdate(TelegramUpdateError):
    """Raised when a supported update has missing or invalid structural fields."""


class UnauthorizedTelegramUpdate(TelegramUpdateError):
    """Raised when an update does not come from the configured private owner chat."""


@dataclass(frozen=True, slots=True)
class TelegramTextInput:
    """One owner-authenticated private text message."""

    update_id: int
    actor_id: int
    chat_id: int
    message_id: int
    text: str = field(repr=False)
    command: str | None = None
    reply_to_message_id: int | None = None


@dataclass(frozen=True, slots=True)
class TelegramVoiceInput:
    """Metadata needed to enqueue one owner-authenticated voice download."""

    update_id: int
    actor_id: int
    chat_id: int
    message_id: int
    file_id: str = field(repr=False)
    file_unique_id: str
    duration_seconds: int
    file_size: int | None
    mime_type: str | None
    reply_to_message_id: int | None = None


@dataclass(frozen=True, slots=True)
class TelegramImageInput:
    """Owner-provided photo or document metadata plus an optional staged file."""

    update_id: int
    actor_id: int
    chat_id: int
    message_id: int
    file_id: str = field(repr=False)
    file_unique_id: str
    file_size: int | None
    mime_type: str
    width: int | None
    height: int | None
    reply_to_message_id: int | None = None
    staged_path: Path | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class TelegramCallbackInput:
    """Opaque callback data whose action is validated by a later workflow layer."""

    update_id: int
    actor_id: int
    chat_id: int
    message_id: int
    query_id: str = field(repr=False)
    data: str = field(repr=False)


TelegramInput = (
    TelegramTextInput | TelegramVoiceInput | TelegramImageInput | TelegramCallbackInput
)


class ProcessedUpdateRepository:
    """Claim Telegram update IDs before dispatch so duplicates have no side effects."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def claim_once(self, *, update_id: int, claimed_at: str) -> bool:
        """Return true only for the first durable claim of a non-negative update ID."""

        if update_id < 0:
            raise ValueError("update_id must not be negative")
        if not claimed_at.strip():
            raise ValueError("claimed_at must not be blank")
        with immediate_transaction(self._connection):
            cursor = self._connection.execute(
                """
                INSERT INTO processed_updates (update_id, processed_at)
                VALUES (?, ?)
                ON CONFLICT (update_id) DO NOTHING
                """,
                (update_id, claimed_at),
            )
            return cursor.rowcount == 1


def parse_telegram_update(payload: object, *, owner_id: int) -> TelegramInput | None:
    """Parse supported updates only after enforcing the numeric owner allowlist."""

    if owner_id <= 0:
        raise ValueError("owner_id must be positive")
    if not isinstance(payload, dict):
        raise MalformedTelegramUpdate("update must be an object")
    update_id = _required_non_negative_int(payload, "update_id")

    message = payload.get("message")
    if message is not None:
        return _parse_message(update_id=update_id, payload=message, owner_id=owner_id)

    callback = payload.get("callback_query")
    if callback is not None:
        return _parse_callback(update_id=update_id, payload=callback, owner_id=owner_id)

    return None


def _parse_message(*, update_id: int, payload: object, owner_id: int) -> TelegramInput:
    if not isinstance(payload, dict):
        raise MalformedTelegramUpdate("message must be an object")
    actor_id, chat_id = _private_owner_identity(payload=payload, owner_id=owner_id)
    message_id = _required_non_negative_int(payload, "message_id")

    text = payload.get("text")
    if isinstance(text, str):
        if not text.strip():
            raise MalformedTelegramUpdate("message text must not be blank")
        return TelegramTextInput(
            update_id=update_id,
            actor_id=actor_id,
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            command=_parse_command(text),
            reply_to_message_id=_reply_to_message_id(payload),
        )

    voice = payload.get("voice")
    if isinstance(voice, dict):
        file_size = voice.get("file_size")
        if file_size is not None and (
            not isinstance(file_size, int) or isinstance(file_size, bool) or file_size < 0
        ):
            raise MalformedTelegramUpdate("voice file_size must not be negative")
        mime_type = voice.get("mime_type")
        if mime_type is not None and (
            not isinstance(mime_type, str) or not mime_type.strip()
        ):
            raise MalformedTelegramUpdate("voice mime_type must not be blank")
        return TelegramVoiceInput(
            update_id=update_id,
            actor_id=actor_id,
            chat_id=chat_id,
            message_id=message_id,
            file_id=_required_text(voice, "file_id"),
            file_unique_id=_required_text(voice, "file_unique_id"),
            duration_seconds=_required_non_negative_int(voice, "duration"),
            file_size=file_size,
            mime_type=mime_type,
            reply_to_message_id=_reply_to_message_id(payload),
        )

    photo = payload.get("photo")
    if isinstance(photo, list):
        return _parse_photo(
            update_id=update_id,
            actor_id=actor_id,
            chat_id=chat_id,
            message_id=message_id,
            payload=photo,
            reply_to_message_id=_reply_to_message_id(payload),
        )

    document = payload.get("document")
    if isinstance(document, dict):
        return TelegramImageInput(
            update_id=update_id,
            actor_id=actor_id,
            chat_id=chat_id,
            message_id=message_id,
            file_id=_required_text(document, "file_id"),
            file_unique_id=_required_text(document, "file_unique_id"),
            file_size=_optional_file_size(document),
            mime_type=_required_text(document, "mime_type"),
            width=None,
            height=None,
            reply_to_message_id=_reply_to_message_id(payload),
        )

    raise MalformedTelegramUpdate("message has no supported text, voice, or image content")


def _parse_photo(
    *,
    update_id: int,
    actor_id: int,
    chat_id: int,
    message_id: int,
    payload: list[object],
    reply_to_message_id: int | None,
) -> TelegramImageInput:
    if not payload:
        raise MalformedTelegramUpdate("photo sizes must not be empty")
    sizes: list[tuple[int, int, int | None, str, str]] = []
    for item in payload:
        if not isinstance(item, dict):
            raise MalformedTelegramUpdate("photo size must be an object")
        width = _required_non_negative_int(item, "width")
        height = _required_non_negative_int(item, "height")
        if width == 0 or height == 0:
            raise MalformedTelegramUpdate("photo dimensions must be positive")
        sizes.append(
            (
                width,
                height,
                _optional_file_size(item),
                _required_text(item, "file_id"),
                _required_text(item, "file_unique_id"),
            )
        )
    width, height, file_size, file_id, file_unique_id = max(
        sizes,
        key=lambda item: (item[0] * item[1], item[2] or 0),
    )
    return TelegramImageInput(
        update_id=update_id,
        actor_id=actor_id,
        chat_id=chat_id,
        message_id=message_id,
        file_id=file_id,
        file_unique_id=file_unique_id,
        file_size=file_size,
        mime_type="image/jpeg",
        width=width,
        height=height,
        reply_to_message_id=reply_to_message_id,
    )


def _optional_file_size(payload: dict[str, object]) -> int | None:
    file_size = payload.get("file_size")
    if file_size is not None and (
        not isinstance(file_size, int)
        or isinstance(file_size, bool)
        or file_size < 0
    ):
        raise MalformedTelegramUpdate("file_size must not be negative")
    return file_size


def _parse_callback(
    *, update_id: int, payload: object, owner_id: int
) -> TelegramCallbackInput:
    if not isinstance(payload, dict):
        raise MalformedTelegramUpdate("callback_query must be an object")
    actor = payload.get("from")
    actor_id = _identity_id(actor, field_name="callback sender")
    if actor_id != owner_id:
        raise UnauthorizedTelegramUpdate("callback sender is not the configured owner")

    message = payload.get("message")
    if not isinstance(message, dict):
        raise MalformedTelegramUpdate("callback must reference a private chat message")
    chat_id = _private_owner_chat(payload=message, owner_id=owner_id)
    message_id = _required_non_negative_int(message, "message_id")
    data = _required_text(payload, "data")
    if len(data.encode("utf-8")) > _MAX_CALLBACK_DATA_BYTES:
        raise MalformedTelegramUpdate("callback data exceeds Telegram's byte limit")
    return TelegramCallbackInput(
        update_id=update_id,
        actor_id=actor_id,
        chat_id=chat_id,
        message_id=message_id,
        query_id=_required_text(payload, "id"),
        data=data,
    )


def _private_owner_identity(*, payload: dict[str, object], owner_id: int) -> tuple[int, int]:
    actor_id = _identity_id(payload.get("from"), field_name="message sender")
    chat_id = _private_owner_chat(payload=payload, owner_id=owner_id)
    if actor_id != owner_id:
        raise UnauthorizedTelegramUpdate("update sender is not the configured owner")
    return actor_id, chat_id


def _private_owner_chat(*, payload: dict[str, object], owner_id: int) -> int:
    chat = payload.get("chat")
    if not isinstance(chat, dict):
        raise MalformedTelegramUpdate("message chat must be an object")
    chat_id = _required_int(chat, "id")
    if chat.get("type") != "private" or chat_id != owner_id:
        raise UnauthorizedTelegramUpdate("update is not from the configured private owner chat")
    return chat_id


def _identity_id(payload: object, *, field_name: str) -> int:
    if not isinstance(payload, dict):
        raise MalformedTelegramUpdate(f"{field_name} must be an object")
    return _required_int(payload, "id")


def _required_text(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MalformedTelegramUpdate(f"{key} must be non-blank text")
    return value


def _reply_to_message_id(payload: dict[str, object]) -> int | None:
    reply = payload.get("reply_to_message")
    if reply is None:
        return None
    if not isinstance(reply, dict):
        raise MalformedTelegramUpdate("reply_to_message must be an object")
    return _required_non_negative_int(reply, "message_id")


def _required_int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise MalformedTelegramUpdate(f"{key} must be an integer")
    return value


def _required_non_negative_int(payload: dict[str, object], key: str) -> int:
    value = _required_int(payload, key)
    if value < 0:
        raise MalformedTelegramUpdate(f"{key} must not be negative")
    return value


def _parse_command(text: str) -> str | None:
    token = text.split(maxsplit=1)[0].lower()
    command = token.split("@", maxsplit=1)[0]
    return command if _COMMAND_PATTERN.fullmatch(command) is not None else None
