"""Official Telegram Bot API transport, polling, downloads, and review delivery."""

# ruff: noqa: RUF001 -- Russian owner-facing interface text is intentional.

import json
import os
import secrets
import sqlite3
import ssl
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast

from praeco.dispatcher import DispatchOutcome, DispatchResult, TelegramDispatcher
from praeco.linkedin import LinkedInOAuthError, LinkedInOAuthService
from praeco.media import ImageValidationError, inspect_image_bytes
from praeco.outbox import DeliveryKind, TelegramDelivery, TelegramSender
from praeco.owner import OwnerDataError, OwnerExporter
from praeco.telegram import (
    TelegramCallbackInput,
    TelegramImageInput,
    TelegramInput,
    TelegramUpdateError,
    TelegramVoiceInput,
    parse_telegram_update,
)
from praeco.worker import staged_voice_path

_MAX_RESPONSE_BYTES = 2_000_000
_MAX_PHOTO_BYTES = 10_000_000
_DEFAULT_MAX_VOICE_BYTES = 20_000_000
_DEFAULT_MAX_OWNER_IMAGE_BYTES = 10_000_000
_MAX_OWNER_EXPORT_BYTES = 45_000_000

OWNER_COMMANDS: tuple[tuple[str, str], ...] = (
    ("start", "Запустить бота и показать подсказку"),
    ("new", "Начать создание нового поста"),
    ("drafts", "Показать последние черновики"),
    ("settings", "Показать настройки и лимиты"),
    ("style", "Управлять правилами текста и визуала"),
    ("costs", "Показать расходы за текущий месяц"),
    ("export", "Скачать архив своих данных"),
    ("linkedin", "Проверить или подключить LinkedIn"),
    ("status", "Проверить состояние текущей работы"),
    ("help", "Показать справку по командам"),
    ("delete_data", "Посмотреть порядок удаления данных"),
)


class TelegramApiError(RuntimeError):
    """Sanitized Telegram failure that never includes tokens or response bodies."""


class TelegramPayloadError(TelegramApiError):
    """Raised when Telegram returns a structurally invalid success payload."""


class MediaRejection(StrEnum):
    """Safe owner-facing categories for locally rejected Telegram media."""

    TOO_LARGE = "TOO_LARGE"
    UNSUPPORTED = "UNSUPPORTED"
    INVALID = "INVALID"


class TelegramMediaError(TelegramApiError):
    """Raised for media the owner can replace without retrying the same update."""

    def __init__(self, reason: MediaRejection) -> None:
        super().__init__(f"telegram media rejected: {reason.value.lower()}")
        self.reason = reason


class TelegramTransport(Protocol):
    """Small injectable transport used by the Bot API client."""

    def call_json(self, *, method: str, payload: Mapping[str, object]) -> object: ...

    def call_multipart(
        self,
        *,
        method: str,
        fields: Mapping[str, str],
        file_field: str,
        file_path: Path,
        mime_type: str,
        max_bytes: int,
    ) -> object: ...

    def download(self, *, file_path: str, max_bytes: int) -> bytes: ...


class UrllibTelegramTransport:
    """Dependency-free HTTPS transport for the official Telegram Bot API."""

    def __init__(self, *, token: str, timeout_seconds: int = 45) -> None:
        if not token.strip():
            raise ValueError("token must not be blank")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._api_base = f"https://api.telegram.org/bot{token}"
        self._file_base = f"https://api.telegram.org/file/bot{token}"
        self._timeout_seconds = timeout_seconds
        self._ssl_context = _ssl_context()

    def call_json(self, *, method: str, payload: Mapping[str, object]) -> object:
        url = f"{self._api_base}/{_safe_method(method)}"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return self._request_json(request)

    def call_multipart(
        self,
        *,
        method: str,
        fields: Mapping[str, str],
        file_field: str,
        file_path: Path,
        mime_type: str,
        max_bytes: int,
    ) -> object:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        size = file_path.stat().st_size
        if size <= 0 or size > max_bytes:
            raise TelegramApiError("telegram upload rejected by local size limit")
        content = file_path.read_bytes()
        if len(content) != size or len(content) > max_bytes:
            raise TelegramApiError("telegram upload file changed during validation")
        boundary = f"praeco-{secrets.token_hex(12)}"
        body = _multipart_body(
            boundary=boundary,
            fields=fields,
            file_field=file_field,
            file_name=file_path.name,
            mime_type=mime_type,
            content=content,
        )
        request = urllib.request.Request(
            f"{self._api_base}/{_safe_method(method)}",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        return self._request_json(request)

    def download(self, *, file_path: str, max_bytes: int) -> bytes:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        safe_path = _safe_remote_file_path(file_path)
        request = urllib.request.Request(
            f"{self._file_base}/{urllib.parse.quote(safe_path, safe='/')}",
            method="GET",
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self._timeout_seconds,
                context=self._ssl_context,
            ) as response:
                content = cast(bytes, response.read(max_bytes + 1))
        except (OSError, TimeoutError, urllib.error.URLError):
            raise TelegramApiError("telegram file download failed") from None
        if len(content) > max_bytes:
            raise TelegramApiError("telegram file exceeds local size limit")
        return content

    def _request_json(self, request: urllib.request.Request) -> object:
        try:
            with urllib.request.urlopen(
                request,
                timeout=self._timeout_seconds,
                context=self._ssl_context,
            ) as response:
                raw = cast(bytes, response.read(_MAX_RESPONSE_BYTES + 1))
        except urllib.error.HTTPError as error:
            raise TelegramApiError(f"telegram HTTP status {error.code}") from None
        except (OSError, TimeoutError, urllib.error.URLError):
            raise TelegramApiError("telegram request failed") from None
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise TelegramApiError("telegram response exceeds local size limit")
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise TelegramPayloadError("telegram returned invalid JSON") from None


class TelegramBotApiClient:
    """Validated methods used by the polling and delivery adapters."""

    def __init__(self, transport: TelegramTransport) -> None:
        self._transport = transport

    def get_updates(
        self,
        *,
        offset: int | None,
        timeout_seconds: int,
        limit: int = 20,
    ) -> tuple[object, ...]:
        if offset is not None and offset < 0:
            raise ValueError("offset must not be negative")
        if not 1 <= timeout_seconds <= 50:
            raise ValueError("timeout_seconds must be between 1 and 50")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        payload: dict[str, object] = {
            "timeout": timeout_seconds,
            "limit": limit,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = self._call_result(method="getUpdates", payload=payload)
        if not isinstance(result, list):
            raise TelegramPayloadError("getUpdates result must be a list")
        return tuple(result)

    def configure_owner_commands(self, *, chat_id: int) -> None:
        """Register the Russian command list and expose Telegram's menu button."""

        if chat_id == 0:
            raise ValueError("chat_id is required")
        commands = [
            {"command": command, "description": description}
            for command, description in OWNER_COMMANDS
        ]
        configured = self._call_result(
            method="setMyCommands",
            payload={
                "commands": commands,
                "scope": {"type": "chat", "chat_id": chat_id},
                "language_code": "ru",
            },
        )
        menu_configured = self._call_result(
            method="setChatMenuButton",
            payload={"chat_id": chat_id, "menu_button": {"type": "commands"}},
        )
        if configured is not True or menu_configured is not True:
            raise TelegramPayloadError("telegram command menu was not accepted")

    def download_file(self, *, file_id: str, max_bytes: int) -> bytes:
        if not file_id.strip():
            raise ValueError("file_id must not be blank")
        result = self._call_result(method="getFile", payload={"file_id": file_id})
        if not isinstance(result, dict):
            raise TelegramPayloadError("getFile result must be an object")
        remote_path = result.get("file_path")
        remote_size = result.get("file_size")
        if not isinstance(remote_path, str) or not remote_path.strip():
            raise TelegramPayloadError("getFile result has no file_path")
        if remote_size is not None and (
            not isinstance(remote_size, int)
            or isinstance(remote_size, bool)
            or remote_size < 0
        ):
            raise TelegramPayloadError("getFile result has invalid file_size")
        if isinstance(remote_size, int) and remote_size > max_bytes:
            raise TelegramApiError("telegram file exceeds local size limit")
        return self._transport.download(file_path=remote_path, max_bytes=max_bytes)

    def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_markup: Mapping[str, object] | None = None,
    ) -> int:
        if chat_id == 0 or not text.strip():
            raise ValueError("chat_id and text are required")
        if len(text) > 4096:
            raise TelegramApiError("telegram text exceeds 4096 characters")
        payload: dict[str, object] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = dict(reply_markup)
        result = self._call_result(method="sendMessage", payload=payload)
        return _message_id(result)

    def send_photo(
        self,
        *,
        chat_id: int,
        photo_path: Path,
        caption: str,
        reply_markup: Mapping[str, object] | None = None,
    ) -> int:
        if chat_id == 0 or not caption.strip():
            raise ValueError("chat_id and caption are required")
        fields = {
            "chat_id": str(chat_id),
            "caption": caption,
        }
        if reply_markup is not None:
            fields["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        result = _telegram_result(
            self._transport.call_multipart(
                method="sendPhoto",
                fields=fields,
                file_field="photo",
                file_path=photo_path,
                mime_type=_image_mime_type(photo_path),
                max_bytes=_MAX_PHOTO_BYTES,
            )
        )
        return _message_id(result)

    def send_document(
        self,
        *,
        chat_id: int,
        content: bytes,
        filename: str,
        caption: str,
    ) -> int:
        if chat_id == 0 or not content or not filename.strip() or not caption.strip():
            raise ValueError("chat_id, content, filename, and caption are required")
        if len(content) > _MAX_OWNER_EXPORT_BYTES:
            raise TelegramApiError("telegram export exceeds local size limit")
        safe_name = Path(filename).name
        if safe_name != filename or not safe_name.endswith(".zip"):
            raise ValueError("export filename must be a safe ZIP name")
        with tempfile.TemporaryDirectory(prefix="praeco-export-") as directory:
            path = Path(directory) / safe_name
            path.write_bytes(content)
            path.chmod(0o600)
            result = _telegram_result(
                self._transport.call_multipart(
                    method="sendDocument",
                    fields={"chat_id": str(chat_id), "caption": caption},
                    file_field="document",
                    file_path=path,
                    mime_type="application/zip",
                    max_bytes=_MAX_OWNER_EXPORT_BYTES,
                )
            )
        return _message_id(result)

    def answer_callback_query(self, *, query_id: str) -> None:
        if not query_id.strip():
            raise ValueError("query_id must not be blank")
        self._call_result(
            method="answerCallbackQuery",
            payload={"callback_query_id": query_id},
        )

    def _call_result(self, *, method: str, payload: Mapping[str, object]) -> object:
        return _telegram_result(
            self._transport.call_json(method=method, payload=payload)
        )


class BotApiTelegramSender(TelegramSender):
    """Render Russian review controls through the official Bot API."""

    def __init__(self, client: TelegramBotApiClient) -> None:
        self._client = client

    def send(self, delivery: TelegramDelivery) -> int:
        if delivery.kind is DeliveryKind.TEXT_REVIEW:
            if (
                delivery.body is None
                or delivery.revision_callback is None
                or delivery.manual_edit_callback is None
            ):
                raise TelegramPayloadError("text review payload is incomplete")
            return self._client.send_message(
                chat_id=delivery.chat_id,
                text=delivery.body,
                reply_markup=_text_review_keyboard(delivery),
            )
        if delivery.kind is DeliveryKind.FINAL_REVIEW:
            if (
                delivery.body is None
                or delivery.image_path is None
                or delivery.image_version is None
                or delivery.cost_usd is None
            ):
                raise TelegramPayloadError("final review payload is incomplete")
            caption = (
                f"{delivery.body}\n\n"
                f"✓ Изображение v{delivery.image_version}\n"
                f"Расход: ${delivery.cost_usd:.6f}\n"
                + (
                    "LinkedIn: готово к публикации"
                    if delivery.publish_callback is not None
                    else "LinkedIn: отключён"
                )
            )
            if len(caption) > 1024:
                raise TelegramPayloadError("final review caption exceeds Telegram limit")
            return self._client.send_photo(
                chat_id=delivery.chat_id,
                photo_path=delivery.image_path,
                caption=caption,
                reply_markup=(
                    {
                        "inline_keyboard": [
                            [
                                {
                                    "text": "Опубликовать в LinkedIn",
                                    "callback_data": delivery.publish_callback,
                                }
                            ]
                        ]
                    }
                    if delivery.publish_callback is not None
                    else None
                ),
            )
        if delivery.kind is DeliveryKind.PUBLICATION_RESULT:
            if delivery.body is None:
                raise TelegramPayloadError("publication result payload is incomplete")
            return self._client.send_message(
                chat_id=delivery.chat_id,
                text=delivery.body,
            )
        if delivery.kind is DeliveryKind.PROCESSING_ALERT:
            if delivery.body is None or delivery.retry_callback is None:
                raise TelegramPayloadError("processing alert payload is incomplete")
            return self._client.send_message(
                chat_id=delivery.chat_id,
                text=delivery.body,
                reply_markup={
                    "inline_keyboard": [
                        [
                            {
                                "text": "Повторить создание текста",
                                "callback_data": delivery.retry_callback,
                            }
                        ]
                    ]
                },
            )
        if delivery.kind is DeliveryKind.PROGRESS_UPDATE:
            if delivery.body is None:
                raise TelegramPayloadError("progress update payload is incomplete")
            return self._client.send_message(
                chat_id=delivery.chat_id,
                text=delivery.body,
            )
        if delivery.image_path is None:
            raise TelegramPayloadError("image review payload is incomplete")
        if (
            delivery.revision_callback is None
            or delivery.owner_upload_callback is None
            or delivery.return_to_text_callback is None
        ):
            raise TelegramPayloadError("image review controls are incomplete")
        return self._client.send_photo(
            chat_id=delivery.chat_id,
            photo_path=delivery.image_path,
            caption="Изображение готово к проверке.",
            reply_markup=_image_review_keyboard(delivery),
        )


class PollingTelegramClient(Protocol):
    """Subset of Bot API operations required by one polling iteration."""

    def get_updates(
        self,
        *,
        offset: int | None,
        timeout_seconds: int,
        limit: int = 20,
    ) -> tuple[object, ...]: ...

    def download_file(self, *, file_id: str, max_bytes: int) -> bytes: ...

    def answer_callback_query(self, *, query_id: str) -> None: ...


class TelegramStatusResponder(Protocol):
    """Send a best-effort owner-facing response for one dispatched update."""

    def respond(self, event: TelegramInput, result: DispatchResult) -> None: ...

    def reject_media(self, event: TelegramInput, reason: MediaRejection) -> None: ...


class RussianStatusResponder:
    """Render concise Russian acknowledgements without echoing private input."""

    def __init__(
        self,
        client: TelegramBotApiClient,
        *,
        paid_ai_enabled: bool = False,
        publishing_enabled: bool = False,
        linkedin_oauth: LinkedInOAuthService | None = None,
        owner_exporter: OwnerExporter | None = None,
        owner_id: int | None = None,
    ) -> None:
        self._client = client
        self._paid_ai_enabled = paid_ai_enabled
        self._publishing_enabled = publishing_enabled
        self._linkedin_oauth = linkedin_oauth
        self._owner_exporter = owner_exporter
        self._owner_id = owner_id

    def respond(self, event: TelegramInput, result: DispatchResult) -> None:
        if result.outcome is DispatchOutcome.EXPORT and result.owner_message is None:
            self._respond_export(event)
            return
        if result.outcome is DispatchOutcome.LINKEDIN_CONNECT:
            self._respond_linkedin(event)
            return
        text = self._message(result)
        if text is None:
            return
        self._client.send_message(chat_id=event.chat_id, text=text)

    def _respond_export(self, event: TelegramInput) -> None:
        if self._owner_exporter is None or self._owner_id is None:
            self._client.send_message(
                chat_id=event.chat_id,
                text="Экспорт пока не настроен на сервере.",
            )
            return
        try:
            content = self._owner_exporter.build_zip(
                owner_id=self._owner_id,
                max_bytes=_MAX_OWNER_EXPORT_BYTES,
            )
        except (OSError, OwnerDataError, sqlite3.Error):
            self._client.send_message(
                chat_id=event.chat_id,
                text=(
                    "Не удалось собрать полный экспорт. Данные не изменены; "
                    "проверьте локальные медиа и повторите позже."
                ),
            )
            return
        self._client.send_document(
            chat_id=event.chat_id,
            content=content,
            filename="praeco-owner-export.zip",
            caption=(
                "Экспорт локальных данных Praeco. Секреты и OAuth-токены не включены."
            ),
        )

    def reject_media(self, event: TelegramInput, reason: MediaRejection) -> None:
        if isinstance(event, TelegramVoiceInput):
            voice_messages = {
                MediaRejection.TOO_LARGE: "Голосовое слишком большое для обработки.",
                MediaRejection.UNSUPPORTED: "Этот формат голосового не поддерживается.",
                MediaRejection.INVALID: "Не удалось скачать голосовое. Отправьте его ещё раз.",
            }
            self._client.send_message(
                chat_id=event.chat_id,
                text=voice_messages[reason],
            )
            return
        messages = {
            MediaRejection.TOO_LARGE: (
                "Файл слишком большой. Отправьте изображение PNG или JPEG до 10 МБ."
            ),
            MediaRejection.UNSUPPORTED: (
                "Поддерживаются только изображения PNG и JPEG."
            ),
            MediaRejection.INVALID: (
                "Не удалось проверить изображение. Отправьте другой PNG или JPEG."
            ),
        }
        self._client.send_message(chat_id=event.chat_id, text=messages[reason])

    def _message(self, result: DispatchResult) -> str | None:
        if result.outcome is DispatchOutcome.DUPLICATE:
            return None
        if result.owner_message is not None:
            return result.owner_message
        if result.outcome is DispatchOutcome.STATUS:
            ai_status = "включены" if self._paid_ai_enabled else "отключены, используются mocks"
            publish_status = "включена" if self._publishing_enabled else "отключена"
            linkedin_status = self._linkedin_status()
            return (
                f"Praeco работает. Реальные AI-вызовы {ai_status}. "
                f"Публикация в LinkedIn {publish_status}. {linkedin_status}"
            )
        if result.outcome is DispatchOutcome.WELCOME:
            ai_status = (
                "реальные AI-вызовы включены"
                if self._paid_ai_enabled
                else "реальные AI-вызовы отключены, используются mocks"
            )
            return (
                f"Praeco готов: {ai_status}; публикация в LinkedIn отключена. "
                "Отправьте мысль текстом или голосовым сообщением."
            )
        messages = {
            DispatchOutcome.HELP: (
                "Отправьте мысль текстом или голосом. Я подготовлю черновик и покажу "
                "кнопки согласования.\n\n"
                "/new — новая мысль\n"
                "/drafts — последние черновики\n"
                "/settings — настройки и лимиты\n"
                "/style — постоянные правила текста и визуала\n"
                "/costs — расходы за текущий месяц\n"
                "/status — режим работы\n"
                "/linkedin — подключение личного профиля\n"
                "/export — архив ваших локальных данных\n"
                "/delete_data — удаление после точного подтверждения."
            ),
            DispatchOutcome.WAITING_FOR_INPUT: "Готов к новой мысли. Пришлите текст или голос.",
            DispatchOutcome.TEXT_JOB_QUEUED: "Мысль принята. Готовлю черновик текста.",
            DispatchOutcome.TRANSCRIPTION_JOB_QUEUED: (
                "Голосовое принято. Сначала расшифрую его, затем подготовлю черновик. "
                "После расшифровки пришлю отдельный статус."
            ),
            DispatchOutcome.REVISION_TRANSCRIPTION_JOB_QUEUED: (
                "Голосовые правки приняты. Сначала расшифрую их, затем подготовлю "
                "новую версию."
            ),
            DispatchOutcome.TEXT_REVISION_REQUESTED: (
                "Напишите правки к этой версии текста одним сообщением."
            ),
            DispatchOutcome.MANUAL_EDIT_REQUESTED: (
                "Пришлите готовый вариант текста одним сообщением. AI для этого не вызывается."
            ),
            DispatchOutcome.TEXT_REVISION_JOB_QUEUED: (
                "Правки приняты. Готовлю новую версию текста."
            ),
            DispatchOutcome.TEXT_MANUALLY_EDITED: (
                "Текст сохранён без AI-вызова. Отправляю новую версию на проверку."
            ),
            DispatchOutcome.IMAGE_JOB_QUEUED: (
                "Текст утверждён. Готовлю одно изображение."
            ),
            DispatchOutcome.IMAGE_REVISION_REQUESTED: (
                "Напишите, что изменить в изображении, одним сообщением."
            ),
            DispatchOutcome.IMAGE_REVISION_JOB_QUEUED: (
                "Правки изображения приняты. Готовлю одну новую версию."
            ),
            DispatchOutcome.OWNER_IMAGE_UPLOAD_REQUESTED: (
                "Пришлите своё изображение как фото или файл. Если важно сохранить "
                "оригинал без сжатия Telegram, отправьте его как файл."
            ),
            DispatchOutcome.OWNER_IMAGE_UPLOAD_EXPECTED: (
                "Жду изображение как фото или файл. Если важен оригинал без сжатия, "
                "отправьте его как файл. Текст не изменил черновик."
            ),
            DispatchOutcome.OWNER_IMAGE_STORED: (
                "Изображение сохранено без AI-вызова и отправлено на проверку."
            ),
            DispatchOutcome.RETURNED_TO_TEXT: (
                "Вернул черновик к тексту. Изображение сохранено в истории."
            ),
            DispatchOutcome.IMAGE_APPROVED: (
                "Изображение утверждено. Отправляю финальную версию."
            ),
            DispatchOutcome.PUBLICATION_QUEUED: (
                "Финальная версия принята. Отправляю в LinkedIn один раз; "
                "автоматического повтора при неизвестном результате не будет."
            ),
            DispatchOutcome.CANCELLED: "Черновик отменён.",
            DispatchOutcome.BUDGET_BLOCKED: (
                "Запрос остановлен лимитом расходов. Платный вызов не выполнен."
            ),
            DispatchOutcome.REJECTED: "Эта версия уже изменена или действие недоступно.",
            DispatchOutcome.UNSUPPORTED: (
                "Команда пока не поддерживается. Используйте /help."
            ),
        }
        message = messages[result.outcome]
        if result.budget_warning:
            return f"{message}\nВнимание: расчёт приблизился к предупреждающему лимиту."
        return message

    def _respond_linkedin(self, event: TelegramInput) -> None:
        if self._linkedin_oauth is None:
            self._client.send_message(
                chat_id=event.chat_id,
                text="Подключение LinkedIn пока не настроено на сервере.",
            )
            return
        try:
            url = self._linkedin_oauth.authorization_url()
        except LinkedInOAuthError:
            self._client.send_message(
                chat_id=event.chat_id,
                text="Не удалось создать ссылку LinkedIn. Попробуйте /linkedin ещё раз позже.",
            )
            return
        self._client.send_message(
            chat_id=event.chat_id,
            text=(
                "Откройте LinkedIn по кнопке и подтвердите доступ. "
                "Ссылка одноразовая и действует 10 минут. Публикация останется отключена."
            ),
            reply_markup={
                "inline_keyboard": [
                    [{"text": "Подключить LinkedIn", "url": url}],
                ]
            },
        )

    def _linkedin_status(self) -> str:
        if self._linkedin_oauth is None:
            return "LinkedIn OAuth не настроен."
        status = self._linkedin_oauth.connection_status()
        if status.connected:
            return f"Личный профиль LinkedIn подключён до {status.expires_at}."
        if status.expires_at is not None:
            return (
                "Подключение LinkedIn истекло или не содержит нужных прав; "
                "используйте /linkedin."
            )
        return "Личный профиль LinkedIn не подключён; используйте /linkedin."


@dataclass(frozen=True, slots=True)
class PollingResult:
    """Non-sensitive summary returned after one bounded polling iteration."""

    next_offset: int | None
    received: int
    dispatched: int
    ignored: int
    response_failures: int = 0
    outcomes: tuple[DispatchOutcome, ...] = field(default_factory=tuple)


class TelegramPollingAdapter:
    """Fetch, validate, stage voice, and transactionally dispatch one update batch."""

    def __init__(
        self,
        *,
        client: PollingTelegramClient,
        dispatcher: TelegramDispatcher,
        owner_id: int,
        media_directory: Path,
        max_voice_bytes: int = _DEFAULT_MAX_VOICE_BYTES,
        max_owner_image_bytes: int = _DEFAULT_MAX_OWNER_IMAGE_BYTES,
        status_responder: TelegramStatusResponder | None = None,
    ) -> None:
        if owner_id <= 0 or max_voice_bytes <= 0 or max_owner_image_bytes <= 0:
            raise ValueError("owner_id and media size limits must be positive")
        self._client = client
        self._dispatcher = dispatcher
        self._owner_id = owner_id
        self._media_directory = media_directory
        self._max_voice_bytes = max_voice_bytes
        self._max_owner_image_bytes = max_owner_image_bytes
        self._status_responder = status_responder

    def poll_once(
        self,
        *,
        offset: int | None,
        timeout_seconds: int,
        now: str,
        billing_month: str,
    ) -> PollingResult:
        updates = self._client.get_updates(
            offset=offset,
            timeout_seconds=timeout_seconds,
        )
        next_offset = offset
        outcomes: list[DispatchOutcome] = []
        ignored = 0
        response_failures = 0
        for payload in updates:
            update_id = _update_id_for_offset(payload)
            try:
                event = parse_telegram_update(payload, owner_id=self._owner_id)
            except TelegramUpdateError:
                ignored += 1
                next_offset = update_id + 1
                continue
            if event is None:
                ignored += 1
                next_offset = update_id + 1
                continue
            staged_path = None
            try:
                if isinstance(event, TelegramVoiceInput):
                    staged_path = self._stage_voice(event)
                elif isinstance(event, TelegramImageInput):
                    event = self._stage_owner_image(event)
                    staged_path = event.staged_path
            except TelegramMediaError as error:
                ignored += 1
                if self._status_responder is not None:
                    try:
                        self._status_responder.reject_media(event, error.reason)
                    except TelegramApiError:
                        response_failures += 1
                next_offset = update_id + 1
                continue
            try:
                result = self._dispatcher.dispatch(
                    event,
                    now=now,
                    billing_month=billing_month,
                )
                outcomes.append(result.outcome)
                if staged_path is not None and result.outcome not in {
                    DispatchOutcome.TRANSCRIPTION_JOB_QUEUED,
                    DispatchOutcome.REVISION_TRANSCRIPTION_JOB_QUEUED,
                    DispatchOutcome.DUPLICATE,
                }:
                    staged_path.unlink(missing_ok=True)
                if isinstance(event, TelegramImageInput) and staged_path is not None:
                    staged_path.unlink(missing_ok=True)
                if isinstance(event, TelegramCallbackInput):
                    self._client.answer_callback_query(query_id=event.query_id)
                if self._status_responder is not None:
                    try:
                        self._status_responder.respond(event, result)
                    except TelegramApiError:
                        response_failures += 1
            except Exception:
                if staged_path is not None:
                    staged_path.unlink(missing_ok=True)
                raise
            next_offset = update_id + 1
        return PollingResult(
            next_offset=next_offset,
            received=len(updates),
            dispatched=len(outcomes),
            ignored=ignored,
            response_failures=response_failures,
            outcomes=tuple(outcomes),
        )

    def _stage_voice(self, event: TelegramVoiceInput) -> Path:
        if event.file_size is not None and event.file_size > self._max_voice_bytes:
            raise TelegramMediaError(MediaRejection.TOO_LARGE)
        content = self._client.download_file(
            file_id=event.file_id,
            max_bytes=self._max_voice_bytes,
        )
        if not content:
            raise TelegramMediaError(MediaRejection.INVALID)
        destination = staged_voice_path(
            media_directory=self._media_directory,
            draft_id=f"tg-{event.update_id}",
        )
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=".voice-",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as temporary_file:
                temporary_file.write(content)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            temporary_path.chmod(0o600)
            temporary_path.replace(destination)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
        return destination

    def _stage_owner_image(self, event: TelegramImageInput) -> TelegramImageInput:
        if (
            event.file_size is not None
            and event.file_size > self._max_owner_image_bytes
        ):
            raise TelegramMediaError(MediaRejection.TOO_LARGE)
        if event.mime_type.lower() not in {"image/jpeg", "image/png"}:
            raise TelegramMediaError(MediaRejection.UNSUPPORTED)
        content = self._client.download_file(
            file_id=event.file_id,
            max_bytes=self._max_owner_image_bytes,
        )
        if not content:
            raise TelegramMediaError(MediaRejection.INVALID)
        try:
            metadata = inspect_image_bytes(
                content,
                declared_mime_type=event.mime_type,
            )
        except ImageValidationError:
            raise TelegramMediaError(MediaRejection.INVALID) from None
        if (
            event.width is not None
            and event.height is not None
            and (event.width, event.height) != (metadata.width, metadata.height)
        ):
            raise TelegramMediaError(MediaRejection.INVALID)
        destination = (
            self._media_directory
            / ".staging"
            / f"owner-image-{event.update_id}{metadata.extension}"
        )
        _atomic_private_write(destination=destination, content=content)
        return replace(
            event,
            mime_type=metadata.mime_type,
            width=metadata.width,
            height=metadata.height,
            staged_path=destination,
        )


def _atomic_private_write(*, destination: Path, content: bytes) -> None:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=".media-",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        temporary_path.chmod(0o600)
        temporary_path.replace(destination)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _telegram_result(payload: object) -> object:
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise TelegramApiError("telegram API rejected the request")
    if "result" not in payload:
        raise TelegramPayloadError("telegram response has no result")
    return payload["result"]


def _message_id(payload: object) -> int:
    if not isinstance(payload, dict):
        raise TelegramPayloadError("telegram message result must be an object")
    message_id = payload.get("message_id")
    if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id < 0:
        raise TelegramPayloadError("telegram message result has invalid message_id")
    return message_id


def _update_id_for_offset(payload: object) -> int:
    if not isinstance(payload, dict):
        raise TelegramPayloadError("telegram update must be an object")
    update_id = payload.get("update_id")
    if not isinstance(update_id, int) or isinstance(update_id, bool) or update_id < 0:
        raise TelegramPayloadError("telegram update has invalid update_id")
    return update_id


def _text_review_keyboard(delivery: TelegramDelivery) -> dict[str, object]:
    return {
        "inline_keyboard": [
            [{"text": "Утвердить текст", "callback_data": delivery.approve_callback}],
            [{"text": "Дать правки", "callback_data": delivery.revision_callback}],
            [
                {
                    "text": "Изменить вручную",
                    "callback_data": delivery.manual_edit_callback,
                }
            ],
            [{"text": "Отменить", "callback_data": delivery.cancel_callback}],
        ]
    }


def _image_review_keyboard(delivery: TelegramDelivery) -> dict[str, object]:
    return {
        "inline_keyboard": [
            [
                {
                    "text": "Утвердить изображение",
                    "callback_data": delivery.approve_callback,
                }
            ],
            [{"text": "Дать правки", "callback_data": delivery.revision_callback}],
            [
                {
                    "text": "Загрузить своё",
                    "callback_data": delivery.owner_upload_callback,
                }
            ],
            [
                {
                    "text": "Вернуться к тексту",
                    "callback_data": delivery.return_to_text_callback,
                }
            ],
            [{"text": "Отменить", "callback_data": delivery.cancel_callback}],
        ]
    }


def _safe_method(method: str) -> str:
    if not method.isascii() or not method.isalnum():
        raise ValueError("Telegram method name is invalid")
    return method


def _safe_remote_file_path(file_path: str) -> str:
    path = Path(file_path)
    if not file_path.strip() or path.is_absolute() or ".." in path.parts:
        raise TelegramPayloadError("Telegram file path is invalid")
    return file_path


def _ssl_context() -> ssl.SSLContext:
    default_paths = ssl.get_default_verify_paths()
    system_cafile = Path("/etc/ssl/cert.pem")
    if default_paths.cafile is None and system_cafile.is_file():
        return ssl.create_default_context(cafile=str(system_cafile))
    return ssl.create_default_context()


def _image_mime_type(path: Path) -> str:
    mime_types = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
    try:
        return mime_types[path.suffix.lower()]
    except KeyError as error:
        raise TelegramPayloadError("unsupported Telegram preview image type") from error


def _multipart_body(
    *,
    boundary: str,
    fields: Mapping[str, str],
    file_field: str,
    file_name: str,
    mime_type: str,
    content: bytes,
) -> bytes:
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            (
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode("utf-8"),
                b"\r\n",
            )
        )
    safe_name = file_name.replace('"', "")
    chunks.extend(
        (
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="{file_field}"; '
                f'filename="{safe_name}"\r\n'
            ).encode(),
            f"Content-Type: {mime_type}\r\n\r\n".encode(),
            content,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        )
    )
    return b"".join(chunks)
