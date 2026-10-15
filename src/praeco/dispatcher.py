"""Transactional Telegram-to-domain dispatch without network or provider calls."""

# ruff: noqa: RUF001 -- Russian owner-facing interface text is intentional.

import hashlib
import re
import sqlite3
from contextlib import suppress
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from praeco.budget import BudgetPolicy
from praeco.drafts import (
    DraftIdentity,
    DraftNotFoundError,
    DraftPersistenceConflict,
    DraftRepository,
    VoiceSource,
)
from praeco.editing import (
    EditingError,
    ImageEditingRepository,
    ImageInputMode,
    PendingImageInput,
    PendingTextInput,
    TextEditingRepository,
    TextInputMode,
    VoiceEditingRepository,
    VoiceRevisionTarget,
)
from praeco.jobs import JobRepository, PaidJobRequest, QueueOutcome
from praeco.outbox import DeliveryKind, DeliveryRepository, DeliveryRequest
from praeco.owner import DraftSummary, OwnerDataError, OwnerRepository, PreferenceRule
from praeco.publication import PublicationError, PublicationRepository
from praeco.storage import immediate_transaction
from praeco.telegram import (
    ProcessedUpdateRepository,
    TelegramCallbackInput,
    TelegramImageInput,
    TelegramInput,
    TelegramTextInput,
    TelegramVoiceInput,
)
from praeco.workflow import UnauthorizedActorError, WorkflowError

_CALLBACK_PATTERN = re.compile(
    r"^(?P<action>txt_ok|txt_upload|txt_rev|txt_edit|txt_cancel|txt_retry|img_ok|img_rev|"
    r"img_upload|img_text|img_cancel|pub):"
    r"(?P<draft_id>[A-Za-z0-9_-]{1,40}):(?P<version>[0-9]{1,9})"
    r"(?::(?P<image_version>[0-9]{1,9}))?$"
)


class DispatchOutcome(StrEnum):
    """Small result vocabulary consumed later by the Telegram delivery adapter."""

    WELCOME = "WELCOME"
    HELP = "HELP"
    STATUS = "STATUS"
    DRAFTS = "DRAFTS"
    SETTINGS = "SETTINGS"
    STYLE = "STYLE"
    COSTS = "COSTS"
    EXPORT = "EXPORT"
    DELETE_DATA = "DELETE_DATA"
    LINKEDIN_CONNECT = "LINKEDIN_CONNECT"
    WAITING_FOR_INPUT = "WAITING_FOR_INPUT"
    TEXT_JOB_QUEUED = "TEXT_JOB_QUEUED"
    TEXT_REVISION_REQUESTED = "TEXT_REVISION_REQUESTED"
    MANUAL_EDIT_REQUESTED = "MANUAL_EDIT_REQUESTED"
    TEXT_REVISION_JOB_QUEUED = "TEXT_REVISION_JOB_QUEUED"
    TEXT_MANUALLY_EDITED = "TEXT_MANUALLY_EDITED"
    OWNER_TEXT_STORED = "OWNER_TEXT_STORED"
    TRANSCRIPTION_JOB_QUEUED = "TRANSCRIPTION_JOB_QUEUED"
    REVISION_TRANSCRIPTION_JOB_QUEUED = "REVISION_TRANSCRIPTION_JOB_QUEUED"
    IMAGE_JOB_QUEUED = "IMAGE_JOB_QUEUED"
    IMAGE_REVISION_REQUESTED = "IMAGE_REVISION_REQUESTED"
    IMAGE_REVISION_JOB_QUEUED = "IMAGE_REVISION_JOB_QUEUED"
    OWNER_IMAGE_UPLOAD_REQUESTED = "OWNER_IMAGE_UPLOAD_REQUESTED"
    OWNER_IMAGE_UPLOAD_EXPECTED = "OWNER_IMAGE_UPLOAD_EXPECTED"
    OWNER_IMAGE_STORED = "OWNER_IMAGE_STORED"
    RETURNED_TO_TEXT = "RETURNED_TO_TEXT"
    IMAGE_APPROVED = "IMAGE_APPROVED"
    PUBLICATION_QUEUED = "PUBLICATION_QUEUED"
    CANCELLED = "CANCELLED"
    BUDGET_BLOCKED = "BUDGET_BLOCKED"
    DUPLICATE = "DUPLICATE"
    REJECTED = "REJECTED"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """Non-sensitive dispatcher result; private content remains in durable storage."""

    outcome: DispatchOutcome
    draft_id: str | None = None
    job_id: int | None = None
    publication_id: int | None = None
    budget_warning: bool = False
    owner_message: str | None = field(default=None, repr=False)
    deleted_media_paths: tuple[str, ...] = field(default=(), repr=False)
    deleted_draft_ids: tuple[str, ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class DispatchCostEstimates:
    """Conservative reservations used before each mocked or real paid stage."""

    text_usd: Decimal
    transcription_usd: Decimal
    image_usd: Decimal

    def __post_init__(self) -> None:
        for field_name in ("text_usd", "transcription_usd", "image_usd"):
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} must not be negative")


@dataclass(frozen=True, slots=True)
class _CallbackAction:
    name: str
    draft_id: str
    version: int
    image_version: int | None = None


class _VoiceReservationBlocked(RuntimeError):
    """Roll back both reservations when a voice revision cannot be fully funded."""


class TelegramDispatcher:
    """Map owner-authenticated Telegram inputs to atomic draft and queue changes."""

    def __init__(
        self,
        *,
        connection: sqlite3.Connection,
        owner_id: int,
        budget_policy: BudgetPolicy,
        cost_estimates: DispatchCostEstimates,
        media_directory: Path | None = None,
        publishing_enabled: bool = False,
    ) -> None:
        if owner_id <= 0:
            raise ValueError("owner_id must be positive")
        self._connection = connection
        self._owner_id = owner_id
        self._budget_policy = budget_policy
        self._cost_estimates = cost_estimates
        self._media_directory = media_directory
        self._updates = ProcessedUpdateRepository(connection)
        self._drafts = DraftRepository(connection)
        self._jobs = JobRepository(connection)
        self._editing = TextEditingRepository(connection)
        self._image_editing = ImageEditingRepository(connection)
        self._voice_editing = VoiceEditingRepository(connection)
        self._deliveries = DeliveryRepository(connection)
        self._owner = OwnerRepository(connection)
        self._publishing_enabled = publishing_enabled
        self._publications = PublicationRepository(connection)

    def dispatch(
        self,
        event: TelegramInput,
        *,
        now: str,
        billing_month: str,
    ) -> DispatchResult:
        """Claim and dispatch one update, rolling back all partial domain changes."""

        if event.actor_id != self._owner_id or event.chat_id != self._owner_id:
            raise UnauthorizedActorError("Telegram event is not owned by the configured user")

        callback: _CallbackAction | None = None
        if isinstance(event, TelegramCallbackInput):
            try:
                callback = _parse_callback_action(event.data)
            except ValueError:
                return self._claim_rejected(event=event, now=now)

        try:
            with immediate_transaction(self._connection):
                if not self._updates.claim_once(update_id=event.update_id, claimed_at=now):
                    result = DispatchResult(outcome=DispatchOutcome.DUPLICATE)
                elif isinstance(event, TelegramTextInput):
                    result = self._dispatch_text(
                        event=event,
                        now=now,
                        billing_month=billing_month,
                    )
                elif isinstance(event, TelegramVoiceInput):
                    result = self._dispatch_voice(
                        event=event,
                        now=now,
                        billing_month=billing_month,
                    )
                elif isinstance(event, TelegramImageInput):
                    result = self._dispatch_image(event=event, now=now)
                else:
                    if callback is None:
                        raise RuntimeError("parsed callback action is missing")
                    result = self._dispatch_callback(
                        event=event,
                        action=callback,
                        now=now,
                        billing_month=billing_month,
                    )
        except (
            DraftNotFoundError,
            DraftPersistenceConflict,
            EditingError,
            WorkflowError,
            PublicationError,
        ):
            return self._claim_rejected(event=event, now=now)
        return self._remove_deleted_media(result)

    def _dispatch_text(
        self,
        *,
        event: TelegramTextInput,
        now: str,
        billing_month: str,
    ) -> DispatchResult:
        if event.command is not None:
            if event.command == "/new" or (
                event.command == "/text" and bool(_command_argument(event.text))
            ):
                active_voice = self._voice_editing.get_active(owner_id=self._owner_id)
                if active_voice is not None:
                    return DispatchResult(
                        outcome=DispatchOutcome.REJECTED,
                        draft_id=active_voice.draft_id,
                    )
                pending = self._editing.get_pending(owner_id=self._owner_id)
                if pending is not None:
                    self._drafts.stop_waiting_for_text_input(
                        draft_id=pending.draft_id,
                        actor_id=event.actor_id,
                        version=pending.base_text_version,
                        now=now,
                    )
                    self._editing.consume(pending=pending)
                pending_image = self._image_editing.get_pending(
                    owner_id=self._owner_id
                )
                if pending_image is not None:
                    self._drafts.stop_waiting_for_image_input(
                        draft_id=pending_image.draft_id,
                        actor_id=event.actor_id,
                        version=pending_image.base_image_version,
                        now=now,
                    )
                    self._image_editing.consume(pending=pending_image)
            owner_command = self._dispatch_owner_command(
                event=event,
                now=now,
                billing_month=billing_month,
            )
            if owner_command is not None:
                return owner_command
            command_outcomes = {
                "/start": DispatchOutcome.WELCOME,
                "/help": DispatchOutcome.HELP,
                "/status": DispatchOutcome.STATUS,
                "/linkedin": DispatchOutcome.LINKEDIN_CONNECT,
                "/new": DispatchOutcome.WAITING_FOR_INPUT,
            }
            return DispatchResult(
                outcome=command_outcomes.get(event.command, DispatchOutcome.UNSUPPORTED)
            )

        active_voice = self._voice_editing.get_active(owner_id=self._owner_id)
        if active_voice is not None:
            return DispatchResult(
                outcome=DispatchOutcome.REJECTED,
                draft_id=active_voice.draft_id,
            )
        pending = self._editing.get_pending(owner_id=self._owner_id)
        if pending is not None:
            if (
                event.reply_to_message_id is not None
                and event.reply_to_message_id != pending.prompt_message_id
            ):
                return DispatchResult(
                    outcome=DispatchOutcome.REJECTED,
                    draft_id=pending.draft_id,
                )
            return self._dispatch_bound_text(
                event=event,
                pending=pending,
                now=now,
                billing_month=billing_month,
            )

        pending_image = self._image_editing.get_pending(owner_id=self._owner_id)
        if pending_image is not None:
            if (
                event.reply_to_message_id is not None
                and event.reply_to_message_id != pending_image.prompt_message_id
            ):
                return DispatchResult(
                    outcome=DispatchOutcome.REJECTED,
                    draft_id=pending_image.draft_id,
                )
            return self._dispatch_bound_image_text(
                event=event,
                pending=pending_image,
                now=now,
                billing_month=billing_month,
            )

        draft_id = _draft_id(event.update_id)
        self._drafts.create(
            identity=DraftIdentity(
                draft_id=draft_id,
                owner_id=self._owner_id,
                source_kind="text",
                source_text=event.text,
                language="ru",
            ),
            now=now,
        )
        queued = self._jobs.enqueue_paid(
            request=PaidJobRequest(
                kind="generate_text",
                draft_id=draft_id,
                input_version=0,
                deduplication_key=f"text:{draft_id}:source",
                estimated_cost_usd=self._cost_estimates.text_usd,
                billing_month=billing_month,
                now=now,
            ),
            policy=self._budget_policy,
        )
        if queued.outcome is QueueOutcome.BUDGET_BLOCKED:
            return DispatchResult(
                outcome=DispatchOutcome.BUDGET_BLOCKED,
                draft_id=draft_id,
            )
        self._drafts.start_text_generation(
            draft_id=draft_id,
            actor_id=self._owner_id,
            now=now,
        )
        return _queued_result(
            outcome=DispatchOutcome.TEXT_JOB_QUEUED,
            draft_id=draft_id,
            queue_outcome=queued.outcome,
            job_id=queued.job_id,
        )

    def _dispatch_owner_command(
        self,
        *,
        event: TelegramTextInput,
        now: str,
        billing_month: str,
    ) -> DispatchResult | None:
        if event.command == "/text":
            body = _command_argument(event.text)
            if not body:
                return DispatchResult(
                    outcome=DispatchOutcome.OWNER_TEXT_STORED,
                    owner_message=(
                        "Пришлите готовый пост одной командой: /text <текст поста>. "
                        "AI для текста вызван не будет."
                    ),
                )
            draft_id = _draft_id(event.update_id)
            self._drafts.create(
                identity=DraftIdentity(
                    draft_id=draft_id,
                    owner_id=self._owner_id,
                    source_kind="text",
                    source_text=body,
                    language="ru",
                ),
                now=now,
            )
            self._drafts.start_text_generation(
                draft_id=draft_id,
                actor_id=self._owner_id,
                now=now,
            )
            self._drafts.record_text(
                draft_id=draft_id,
                actor_id=self._owner_id,
                version=1,
                body=body,
                revision_comment=None,
                prompt_version=None,
                style_version=None,
                provider_model=None,
                image_brief="Derive the visual concept from the approved owner text.",
                now=now,
            )
            self._deliveries.enqueue(
                DeliveryRequest(
                    kind=DeliveryKind.TEXT_REVIEW,
                    draft_id=draft_id,
                    chat_id=event.chat_id,
                    text_version=1,
                    deduplication_key=f"telegram:text-review:{draft_id}:1",
                    now=now,
                )
            )
            return DispatchResult(
                outcome=DispatchOutcome.OWNER_TEXT_STORED,
                draft_id=draft_id,
            )
        if event.command == "/drafts":
            drafts = self._owner.recent_drafts(owner_id=self._owner_id)
            return DispatchResult(
                outcome=DispatchOutcome.DRAFTS,
                owner_message=_render_drafts(drafts),
            )
        if event.command == "/settings":
            return DispatchResult(
                outcome=DispatchOutcome.SETTINGS,
                owner_message=_render_settings(self._budget_policy),
            )
        if event.command == "/costs":
            summary = self._owner.cost_summary(billing_month=billing_month)
            return DispatchResult(
                outcome=DispatchOutcome.COSTS,
                owner_message=(
                    f"Расходы за {billing_month}:\n"
                    f"Учтено: ${summary.accounted_usd:.6f}\n"
                    f"Зарезервировано: ${summary.reserved_usd:.6f}\n"
                    f"Завершённых вызовов: {summary.completed_calls}\n"
                    f"Вызовов с неизвестным точным списанием: "
                    f"{summary.unknown_billed_calls}\n"
                    f"Лимит месяца: ${self._budget_policy.monthly_hard_limit_usd:.2f}"
                ),
            )
        if event.command == "/export":
            if _command_argument(event.text):
                return DispatchResult(
                    outcome=DispatchOutcome.EXPORT,
                    owner_message="Используйте команду без параметров: /export",
                )
            return DispatchResult(outcome=DispatchOutcome.EXPORT)
        if event.command == "/delete_data":
            argument = _command_argument(event.text)
            if argument != "CONFIRM":
                return DispatchResult(
                    outcome=DispatchOutcome.DELETE_DATA,
                    owner_message=(
                        "Удаление затронет все локальные черновики, версии, изображения, "
                        "правила, историю расходов и подключение LinkedIn. "
                        "Опубликованные посты в LinkedIn не удалятся.\n"
                        "Для необратимого удаления отправьте точно: /delete_data CONFIRM"
                    ),
                )
            plan = self._owner.delete_all_owner_data(owner_id=self._owner_id)
            return DispatchResult(
                outcome=DispatchOutcome.DELETE_DATA,
                owner_message=(
                    "Локальные данные Praeco удалены. Опубликованные посты в LinkedIn "
                    "не изменялись. Для новых публикаций подключите LinkedIn заново."
                ),
                deleted_media_paths=plan.media_paths,
                deleted_draft_ids=plan.draft_ids,
            )
        if event.command != "/style":
            return None
        return self._dispatch_style_command(event=event, now=now)

    def _dispatch_style_command(
        self,
        *,
        event: TelegramTextInput,
        now: str,
    ) -> DispatchResult:
        parts = event.text.strip().split(maxsplit=2)
        if len(parts) == 1:
            return DispatchResult(
                outcome=DispatchOutcome.STYLE,
                owner_message=_render_preferences(self._owner.preferences()),
            )
        if len(parts) != 3:
            return DispatchResult(
                outcome=DispatchOutcome.STYLE,
                owner_message=_style_usage(),
            )
        action = parts[1].casefold()
        value = parts[2]
        try:
            if action in {"text", "image"}:
                preference = self._owner.add_preference(
                    scope=action,
                    rule=value,
                    now=now,
                )
                return DispatchResult(
                    outcome=DispatchOutcome.STYLE,
                    owner_message=(
                        f"Правило #{preference.rule_id} сохранено для "
                        f"{'текста' if action == 'text' else 'визуала'}. "
                        "Оно применяется только к будущим генерациям."
                    ),
                )
            if action in {"off", "on"}:
                preference = self._owner.set_preference_active(
                    rule_id=int(value),
                    active=action == "on",
                )
                state = "включено" if preference.active else "отключено"
                return DispatchResult(
                    outcome=DispatchOutcome.STYLE,
                    owner_message=f"Правило #{preference.rule_id} {state}.",
                )
        except (OwnerDataError, ValueError):
            return DispatchResult(
                outcome=DispatchOutcome.STYLE,
                owner_message=(
                    "Не удалось изменить правила. Проверьте ID, дубликаты и длину "
                    "правила (до 500 символов).\n" + _style_usage()
                ),
            )
        return DispatchResult(
            outcome=DispatchOutcome.STYLE,
            owner_message=_style_usage(),
        )

    def _remove_deleted_media(self, result: DispatchResult) -> DispatchResult:
        if not result.deleted_media_paths and not result.deleted_draft_ids:
            return result
        if self._media_directory is None:
            return replace(
                result,
                owner_message=(
                    "Данные в БД удалены, но каталог медиа не настроен для очистки."
                ),
            )
        media_root = self._media_directory.resolve()
        candidates = [media_root / path for path in result.deleted_media_paths]
        candidates.extend(
            media_root
            / "incoming"
            / f"{hashlib.sha256(draft_id.encode('utf-8')).hexdigest()[:20]}.ogg"
            for draft_id in result.deleted_draft_ids
        )
        failures = 0
        parents: set[Path] = set()
        for candidate in candidates:
            resolved = candidate.resolve()
            if not resolved.is_relative_to(media_root):
                failures += 1
                continue
            parents.add(resolved.parent)
            try:
                resolved.unlink(missing_ok=True)
            except OSError:
                failures += 1
        for parent in sorted(parents, key=lambda path: len(path.parts), reverse=True):
            if parent == media_root:
                continue
            with suppress(OSError):
                parent.rmdir()
        if failures:
            return replace(
                result,
                owner_message=(
                    "Данные в БД удалены, но часть локальных медиа не удалось удалить. "
                    "Проверьте каталог хранения перед следующим backup."
                ),
            )
        return result

    def _dispatch_voice(
        self,
        *,
        event: TelegramVoiceInput,
        now: str,
        billing_month: str,
    ) -> DispatchResult:
        active_voice = self._voice_editing.get_active(owner_id=self._owner_id)
        if active_voice is not None:
            return DispatchResult(
                outcome=DispatchOutcome.REJECTED,
                draft_id=active_voice.draft_id,
            )
        pending = self._editing.get_pending(owner_id=self._owner_id)
        pending_image = self._image_editing.get_pending(owner_id=self._owner_id)
        if pending is not None:
            if pending.mode is not TextInputMode.AI_REVISION:
                return DispatchResult(
                    outcome=DispatchOutcome.UNSUPPORTED,
                    draft_id=pending.draft_id,
                )
            if (
                event.reply_to_message_id is not None
                and event.reply_to_message_id != pending.prompt_message_id
            ):
                return DispatchResult(
                    outcome=DispatchOutcome.REJECTED,
                    draft_id=pending.draft_id,
                )
            return self._dispatch_bound_voice_revision(
                event=event,
                pending_text=pending,
                pending_image=None,
                now=now,
                billing_month=billing_month,
            )
        if pending_image is not None:
            if pending_image.mode is not ImageInputMode.AI_REVISION:
                return DispatchResult(
                    outcome=DispatchOutcome.UNSUPPORTED,
                    draft_id=pending_image.draft_id,
                )
            if (
                event.reply_to_message_id is not None
                and event.reply_to_message_id != pending_image.prompt_message_id
            ):
                return DispatchResult(
                    outcome=DispatchOutcome.REJECTED,
                    draft_id=pending_image.draft_id,
                )
            return self._dispatch_bound_voice_revision(
                event=event,
                pending_text=None,
                pending_image=pending_image,
                now=now,
                billing_month=billing_month,
            )
        draft_id = _draft_id(event.update_id)
        self._drafts.create(
            identity=DraftIdentity(
                draft_id=draft_id,
                owner_id=self._owner_id,
                source_kind="voice",
                source_text="",
                language="und",
                voice_source=VoiceSource(
                    file_id=event.file_id,
                    file_unique_id=event.file_unique_id,
                    duration_seconds=event.duration_seconds,
                    file_size=event.file_size,
                    mime_type=event.mime_type or "audio/ogg",
                ),
            ),
            now=now,
        )
        queued = self._jobs.enqueue_paid(
            request=PaidJobRequest(
                kind="transcribe_voice",
                draft_id=draft_id,
                input_version=0,
                deduplication_key=f"transcription:{draft_id}:source",
                estimated_cost_usd=self._cost_estimates.transcription_usd,
                billing_month=billing_month,
                now=now,
            ),
            policy=self._budget_policy,
        )
        if queued.outcome is QueueOutcome.BUDGET_BLOCKED:
            return DispatchResult(
                outcome=DispatchOutcome.BUDGET_BLOCKED,
                draft_id=draft_id,
            )
        return _queued_result(
            outcome=DispatchOutcome.TRANSCRIPTION_JOB_QUEUED,
            draft_id=draft_id,
            queue_outcome=queued.outcome,
            job_id=queued.job_id,
        )

    def _dispatch_bound_voice_revision(
        self,
        *,
        event: TelegramVoiceInput,
        pending_text: PendingTextInput | None,
        pending_image: PendingImageInput | None,
        now: str,
        billing_month: str,
    ) -> DispatchResult:
        if (pending_text is None) == (pending_image is None):
            raise RuntimeError("voice revision requires exactly one pending binding")
        if pending_text is not None:
            draft_id = pending_text.draft_id
            target = VoiceRevisionTarget.TEXT
            base_text_version = pending_text.base_text_version
            base_image_version = None
            prompt_message_id = pending_text.prompt_message_id
            generation_kind = "generate_text"
            generation_cost = self._cost_estimates.text_usd
            generation_key = (
                f"text:{draft_id}:voice-revision:{base_text_version}:"
                f"update:{event.update_id}"
            )
        else:
            image_binding = _required_pending_image(pending_image)
            draft_id = image_binding.draft_id
            target = VoiceRevisionTarget.IMAGE
            base_text_version = image_binding.source_text_version
            base_image_version = image_binding.base_image_version
            prompt_message_id = image_binding.prompt_message_id
            generation_kind = "generate_image"
            generation_cost = self._cost_estimates.image_usd
            generation_key = (
                f"image:{draft_id}:voice-revision:{base_image_version}:"
                f"update:{event.update_id}"
            )
        try:
            with immediate_transaction(self._connection):
                transcription = self._jobs.enqueue_paid(
                    request=PaidJobRequest(
                        kind="transcribe_revision",
                        draft_id=draft_id,
                        input_version=base_text_version,
                        deduplication_key=(
                            f"transcription:{draft_id}:voice-revision:"
                            f"update:{event.update_id}"
                        ),
                        estimated_cost_usd=self._cost_estimates.transcription_usd,
                        billing_month=billing_month,
                        now=now,
                    ),
                    policy=self._budget_policy,
                )
                if transcription.outcome is QueueOutcome.BUDGET_BLOCKED:
                    raise _VoiceReservationBlocked
                generation = self._jobs.enqueue_paid(
                    request=PaidJobRequest(
                        kind=generation_kind,
                        draft_id=draft_id,
                        input_version=base_text_version,
                        deduplication_key=generation_key,
                        estimated_cost_usd=generation_cost,
                        billing_month=billing_month,
                        now=now,
                    ),
                    policy=self._budget_policy,
                )
                if generation.outcome is QueueOutcome.BUDGET_BLOCKED:
                    raise _VoiceReservationBlocked
                if transcription.job_id is None or generation.job_id is None:
                    raise RuntimeError("voice revision queue returned no job ID")
                self._jobs.add_dependency(
                    job_id=generation.job_id,
                    prerequisite_job_id=transcription.job_id,
                    now=now,
                )
                self._voice_editing.record(
                    transcription_job_id=transcription.job_id,
                    generation_job_id=generation.job_id,
                    owner_id=event.actor_id,
                    draft_id=draft_id,
                    target=target,
                    base_text_version=base_text_version,
                    base_image_version=base_image_version,
                    prompt_message_id=prompt_message_id,
                    telegram_update_id=event.update_id,
                    file_unique_id=event.file_unique_id,
                    duration_seconds=event.duration_seconds,
                    file_size=event.file_size,
                    mime_type=event.mime_type or "audio/ogg",
                    now=now,
                )
        except _VoiceReservationBlocked:
            return DispatchResult(
                outcome=DispatchOutcome.BUDGET_BLOCKED,
                draft_id=draft_id,
            )
        return DispatchResult(
            outcome=DispatchOutcome.REVISION_TRANSCRIPTION_JOB_QUEUED,
            draft_id=draft_id,
            job_id=transcription.job_id,
            budget_warning=(
                transcription.outcome is QueueOutcome.CREATED_WITH_WARNING
                or generation.outcome is QueueOutcome.CREATED_WITH_WARNING
            ),
        )

    def _dispatch_image(
        self,
        *,
        event: TelegramImageInput,
        now: str,
    ) -> DispatchResult:
        active_voice = self._voice_editing.get_active(owner_id=self._owner_id)
        if active_voice is not None:
            return DispatchResult(
                outcome=DispatchOutcome.REJECTED,
                draft_id=active_voice.draft_id,
            )
        pending = self._image_editing.get_pending(owner_id=self._owner_id)
        if pending is None or pending.mode is not ImageInputMode.OWNER_UPLOAD:
            return DispatchResult(outcome=DispatchOutcome.UNSUPPORTED)
        if (
            event.reply_to_message_id is not None
            and event.reply_to_message_id != pending.prompt_message_id
        ):
            return DispatchResult(
                outcome=DispatchOutcome.REJECTED,
                draft_id=pending.draft_id,
            )
        if (
            self._media_directory is None
            or event.staged_path is None
            or event.width is None
            or event.height is None
        ):
            raise DraftPersistenceConflict("owner image was not staged and validated")
        workflow = self._drafts.get(pending.draft_id)
        version = workflow.current_image_version + 1
        extension = ".png" if event.mime_type == "image/png" else ".jpg"
        relative_path = Path(pending.draft_id) / f"owner-v{version}{extension}"
        media_root = self._media_directory.resolve()
        staged_path = event.staged_path.resolve()
        if not staged_path.is_relative_to(media_root):
            raise DraftPersistenceConflict("staged owner image escapes media directory")
        final_path = (media_root / relative_path).resolve()
        if not final_path.is_relative_to(media_root) or final_path.exists():
            raise DraftPersistenceConflict("owner image destination is invalid")
        final_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        final_path.parent.chmod(0o700)
        staged_path.replace(final_path)
        final_path.chmod(0o600)
        try:
            with final_path.open("rb") as image_file:
                sha256 = hashlib.file_digest(image_file, "sha256").hexdigest()
            if pending.base_image_version == 0:
                source_text = self._drafts.get_text_version(
                    draft_id=pending.draft_id,
                    version=pending.source_text_version,
                )
                brief = source_text.image_brief or source_text.body
            else:
                base_image = self._drafts.get_image_version(
                    draft_id=pending.draft_id,
                    version=pending.base_image_version,
                )
                brief = base_image.brief
            self._drafts.record_owner_image(
                draft_id=pending.draft_id,
                actor_id=event.actor_id,
                version=version,
                source_text_version=pending.source_text_version,
                brief=brief,
                local_path=str(relative_path),
                sha256=sha256,
                mime_type=event.mime_type,
                width=event.width,
                height=event.height,
                now=now,
            )
            self._image_editing.consume(pending=pending)
            self._deliveries.enqueue(
                DeliveryRequest(
                    kind=DeliveryKind.IMAGE_REVIEW,
                    draft_id=pending.draft_id,
                    chat_id=event.chat_id,
                    image_version=version,
                    deduplication_key=(
                        f"telegram:image-review:{pending.draft_id}:{version}"
                    ),
                    now=now,
                )
            )
        except BaseException:
            final_path.unlink(missing_ok=True)
            raise
        return DispatchResult(
            outcome=DispatchOutcome.OWNER_IMAGE_STORED,
            draft_id=pending.draft_id,
        )

    def _dispatch_callback(
        self,
        *,
        event: TelegramCallbackInput,
        action: _CallbackAction,
        now: str,
        billing_month: str,
    ) -> DispatchResult:
        if action.name == "pub":
            if not self._publishing_enabled or action.image_version is None:
                raise PublicationError("publishing is disabled")
            publication_id = self._publications.enqueue(
                draft_id=action.draft_id,
                actor_id=event.actor_id,
                text_version=action.version,
                image_version=action.image_version,
                now=now,
            )
            return DispatchResult(
                outcome=DispatchOutcome.PUBLICATION_QUEUED,
                draft_id=action.draft_id,
                publication_id=publication_id,
            )
        active_voice = self._voice_editing.get_active(owner_id=self._owner_id)
        if active_voice is not None:
            return DispatchResult(
                outcome=DispatchOutcome.REJECTED,
                draft_id=active_voice.draft_id,
            )
        if action.name in {"txt_rev", "txt_edit"}:
            mode = (
                TextInputMode.AI_REVISION
                if action.name == "txt_rev"
                else TextInputMode.MANUAL_EDIT
            )
            self._drafts.wait_for_text_input(
                draft_id=action.draft_id,
                actor_id=event.actor_id,
                version=action.version,
                now=now,
            )
            self._editing.bind(
                owner_id=event.actor_id,
                draft_id=action.draft_id,
                base_text_version=action.version,
                mode=mode,
                prompt_message_id=event.message_id,
                now=now,
            )
            return DispatchResult(
                outcome=(
                    DispatchOutcome.TEXT_REVISION_REQUESTED
                    if mode is TextInputMode.AI_REVISION
                    else DispatchOutcome.MANUAL_EDIT_REQUESTED
                ),
                draft_id=action.draft_id,
            )

        if action.name == "txt_retry":
            failed_job = self._connection.execute(
                """
                SELECT kind, state
                FROM jobs WHERE id = ? AND draft_id = ?
                """,
                (action.version, action.draft_id),
            ).fetchone()
            workflow = self._drafts.get(action.draft_id)
            if (
                failed_job is None
                or str(failed_job["kind"]) != "generate_text"
                or str(failed_job["state"]) != "RESULT_UNKNOWN"
                or workflow.state.value != "FAILED_RECOVERABLE"
            ):
                raise DraftPersistenceConflict("text retry is unavailable")
            queued = self._jobs.enqueue_paid(
                request=PaidJobRequest(
                    kind="generate_text",
                    draft_id=action.draft_id,
                    input_version=workflow.current_text_version,
                    deduplication_key=(
                        f"text-retry:{action.draft_id}:after:{action.version}"
                    ),
                    estimated_cost_usd=self._cost_estimates.text_usd,
                    billing_month=billing_month,
                    now=now,
                ),
                policy=self._budget_policy,
            )
            if queued.outcome is QueueOutcome.BUDGET_BLOCKED:
                return DispatchResult(
                    outcome=DispatchOutcome.BUDGET_BLOCKED,
                    draft_id=action.draft_id,
                )
            self._drafts.start_text_generation(
                draft_id=action.draft_id,
                actor_id=event.actor_id,
                now=now,
            )
            return _queued_result(
                outcome=DispatchOutcome.TEXT_JOB_QUEUED,
                draft_id=action.draft_id,
                queue_outcome=queued.outcome,
                job_id=queued.job_id,
            )

        if action.name == "txt_ok":
            self._drafts.approve_text(
                draft_id=action.draft_id,
                actor_id=event.actor_id,
                version=action.version,
                now=now,
            )
            queued = self._jobs.enqueue_paid(
                request=PaidJobRequest(
                    kind="generate_image",
                    draft_id=action.draft_id,
                    input_version=action.version,
                    deduplication_key=(
                        f"image:{action.draft_id}:text:{action.version}:first"
                    ),
                    estimated_cost_usd=self._cost_estimates.image_usd,
                    billing_month=billing_month,
                    now=now,
                ),
                policy=self._budget_policy,
            )
            if queued.outcome is QueueOutcome.BUDGET_BLOCKED:
                return DispatchResult(
                    outcome=DispatchOutcome.BUDGET_BLOCKED,
                    draft_id=action.draft_id,
                )
            self._drafts.start_image_generation(
                draft_id=action.draft_id,
                actor_id=event.actor_id,
                now=now,
            )
            return _queued_result(
                outcome=DispatchOutcome.IMAGE_JOB_QUEUED,
                draft_id=action.draft_id,
                queue_outcome=queued.outcome,
                job_id=queued.job_id,
            )

        if action.name == "txt_upload":
            workflow = self._drafts.approve_text(
                draft_id=action.draft_id,
                actor_id=event.actor_id,
                version=action.version,
                now=now,
            )
            self._drafts.wait_for_image_input(
                draft_id=action.draft_id,
                actor_id=event.actor_id,
                version=workflow.current_image_version,
                now=now,
            )
            self._image_editing.bind(
                owner_id=event.actor_id,
                draft_id=action.draft_id,
                base_image_version=workflow.current_image_version,
                source_text_version=workflow.current_text_version,
                mode=ImageInputMode.OWNER_UPLOAD,
                prompt_message_id=event.message_id,
                now=now,
            )
            return DispatchResult(
                outcome=DispatchOutcome.OWNER_IMAGE_UPLOAD_REQUESTED,
                draft_id=action.draft_id,
            )

        if action.name == "img_ok":
            workflow = self._drafts.approve_image(
                draft_id=action.draft_id,
                actor_id=event.actor_id,
                version=action.version,
                now=now,
            )
            if workflow.approved_text_version is None:
                raise DraftPersistenceConflict("final snapshot has no approved text")
            self._deliveries.enqueue(
                DeliveryRequest(
                    kind=DeliveryKind.FINAL_REVIEW,
                    draft_id=action.draft_id,
                    chat_id=event.chat_id,
                    text_version=workflow.approved_text_version,
                    image_version=action.version,
                    deduplication_key=(
                        f"telegram:final-review:{action.draft_id}:"
                        f"{workflow.approved_text_version}:{action.version}"
                    ),
                    now=now,
                )
            )
            return DispatchResult(
                outcome=DispatchOutcome.IMAGE_APPROVED,
                draft_id=action.draft_id,
            )

        if action.name in {"img_rev", "img_upload"}:
            workflow = self._drafts.wait_for_image_input(
                draft_id=action.draft_id,
                actor_id=event.actor_id,
                version=action.version,
                now=now,
            )
            if workflow.image_source_text_version is None:
                raise DraftPersistenceConflict("image has no source text version")
            image_mode = (
                ImageInputMode.AI_REVISION
                if action.name == "img_rev"
                else ImageInputMode.OWNER_UPLOAD
            )
            self._image_editing.bind(
                owner_id=event.actor_id,
                draft_id=action.draft_id,
                base_image_version=action.version,
                source_text_version=workflow.image_source_text_version,
                mode=image_mode,
                prompt_message_id=event.message_id,
                now=now,
            )
            return DispatchResult(
                outcome=(
                    DispatchOutcome.IMAGE_REVISION_REQUESTED
                    if image_mode is ImageInputMode.AI_REVISION
                    else DispatchOutcome.OWNER_IMAGE_UPLOAD_REQUESTED
                ),
                draft_id=action.draft_id,
            )

        if action.name == "img_text":
            workflow = self._drafts.return_to_text_review(
                draft_id=action.draft_id,
                actor_id=event.actor_id,
                image_version=action.version,
                now=now,
            )
            self._deliveries.enqueue(
                DeliveryRequest(
                    kind=DeliveryKind.TEXT_REVIEW,
                    draft_id=action.draft_id,
                    chat_id=event.chat_id,
                    text_version=workflow.current_text_version,
                    deduplication_key=(
                        f"telegram:text-review:{action.draft_id}:"
                        f"{workflow.current_text_version}:return:{event.update_id}"
                    ),
                    now=now,
                )
            )
            return DispatchResult(
                outcome=DispatchOutcome.RETURNED_TO_TEXT,
                draft_id=action.draft_id,
            )

        self._drafts.cancel(
            draft_id=action.draft_id,
            actor_id=event.actor_id,
            expected_text_version=(
                action.version if action.name == "txt_cancel" else None
            ),
            expected_image_version=(
                action.version if action.name == "img_cancel" else None
            ),
            now=now,
        )
        pending = self._editing.get_pending(owner_id=event.actor_id)
        if (
            pending is not None
            and pending.draft_id == action.draft_id
        ):
            self._editing.consume(pending=pending)
        pending_image = self._image_editing.get_pending(owner_id=event.actor_id)
        if (
            pending_image is not None
            and pending_image.draft_id == action.draft_id
        ):
            self._image_editing.consume(pending=pending_image)
        return DispatchResult(
            outcome=DispatchOutcome.CANCELLED,
            draft_id=action.draft_id,
        )

    def _dispatch_bound_text(
        self,
        *,
        event: TelegramTextInput,
        pending: PendingTextInput,
        now: str,
        billing_month: str,
    ) -> DispatchResult:
        if pending.mode is TextInputMode.MANUAL_EDIT:
            new_version = pending.base_text_version + 1
            self._drafts.record_text(
                draft_id=pending.draft_id,
                actor_id=event.actor_id,
                version=new_version,
                body=event.text,
                revision_comment=None,
                prompt_version=None,
                style_version=None,
                provider_model=None,
                image_brief=event.text,
                now=now,
            )
            self._editing.consume(pending=pending)
            self._deliveries.enqueue(
                DeliveryRequest(
                    kind=DeliveryKind.TEXT_REVIEW,
                    draft_id=pending.draft_id,
                    chat_id=event.chat_id,
                    text_version=new_version,
                    deduplication_key=(
                        f"telegram:text-review:{pending.draft_id}:{new_version}"
                    ),
                    now=now,
                )
            )
            return DispatchResult(
                outcome=DispatchOutcome.TEXT_MANUALLY_EDITED,
                draft_id=pending.draft_id,
            )

        queued = self._jobs.enqueue_paid(
            request=PaidJobRequest(
                kind="generate_text",
                draft_id=pending.draft_id,
                input_version=pending.base_text_version,
                deduplication_key=(
                    f"text:{pending.draft_id}:revision:{pending.base_text_version}:"
                    f"update:{event.update_id}"
                ),
                estimated_cost_usd=self._cost_estimates.text_usd,
                billing_month=billing_month,
                now=now,
            ),
            policy=self._budget_policy,
        )
        if queued.outcome is QueueOutcome.BUDGET_BLOCKED:
            return DispatchResult(
                outcome=DispatchOutcome.BUDGET_BLOCKED,
                draft_id=pending.draft_id,
            )
        if queued.job_id is None:
            raise RuntimeError("revision queue returned no job ID")
        self._editing.record_revision_request(
            job_id=queued.job_id,
            pending=pending,
            comment=event.text,
            source_kind="text",
            now=now,
        )
        self._editing.consume(pending=pending)
        self._drafts.start_text_generation(
            draft_id=pending.draft_id,
            actor_id=event.actor_id,
            now=now,
        )
        return _queued_result(
            outcome=DispatchOutcome.TEXT_REVISION_JOB_QUEUED,
            draft_id=pending.draft_id,
            queue_outcome=queued.outcome,
            job_id=queued.job_id,
        )

    def _dispatch_bound_image_text(
        self,
        *,
        event: TelegramTextInput,
        pending: PendingImageInput,
        now: str,
        billing_month: str,
    ) -> DispatchResult:
        if pending.mode is ImageInputMode.OWNER_UPLOAD:
            return DispatchResult(
                outcome=DispatchOutcome.OWNER_IMAGE_UPLOAD_EXPECTED,
                draft_id=pending.draft_id,
            )
        queued = self._jobs.enqueue_paid(
            request=PaidJobRequest(
                kind="generate_image",
                draft_id=pending.draft_id,
                input_version=pending.source_text_version,
                deduplication_key=(
                    f"image:{pending.draft_id}:revision:{pending.base_image_version}:"
                    f"update:{event.update_id}"
                ),
                estimated_cost_usd=self._cost_estimates.image_usd,
                billing_month=billing_month,
                now=now,
            ),
            policy=self._budget_policy,
        )
        if queued.outcome is QueueOutcome.BUDGET_BLOCKED:
            return DispatchResult(
                outcome=DispatchOutcome.BUDGET_BLOCKED,
                draft_id=pending.draft_id,
            )
        if queued.job_id is None:
            raise RuntimeError("image revision queue returned no job ID")
        self._image_editing.record_revision_request(
            job_id=queued.job_id,
            pending=pending,
            comment=event.text,
            now=now,
        )
        self._image_editing.consume(pending=pending)
        self._drafts.start_image_generation(
            draft_id=pending.draft_id,
            actor_id=event.actor_id,
            now=now,
        )
        return _queued_result(
            outcome=DispatchOutcome.IMAGE_REVISION_JOB_QUEUED,
            draft_id=pending.draft_id,
            queue_outcome=queued.outcome,
            job_id=queued.job_id,
        )

    def _claim_rejected(
        self,
        *,
        event: TelegramInput,
        now: str,
    ) -> DispatchResult:
        if not self._updates.claim_once(update_id=event.update_id, claimed_at=now):
            return DispatchResult(outcome=DispatchOutcome.DUPLICATE)
        return DispatchResult(outcome=DispatchOutcome.REJECTED)


def _parse_callback_action(data: str) -> _CallbackAction:
    match = _CALLBACK_PATTERN.fullmatch(data)
    if match is None:
        raise ValueError("unsupported callback format")
    version = int(match.group("version"))
    image_version_raw = match.group("image_version")
    action = _CallbackAction(
        name=match.group("action"),
        draft_id=match.group("draft_id"),
        version=version,
        image_version=(int(image_version_raw) if image_version_raw else None),
    )
    if (action.name == "pub") != (action.image_version is not None):
        raise ValueError("publication callback version shape is invalid")
    return action


def _draft_id(update_id: int) -> str:
    return f"tg-{update_id}"


def _command_argument(text: str) -> str:
    parts = text.strip().split(maxsplit=1)
    return parts[1].strip() if len(parts) == 2 else ""


def _render_drafts(drafts: tuple[DraftSummary, ...]) -> str:
    if not drafts:
        return "Черновиков пока нет. Отправьте новую мысль текстом или голосом."
    state_labels = {
        "RECEIVED": "принят",
        "TEXT_GENERATING": "создаётся текст",
        "TEXT_REVIEW": "проверка текста",
        "TEXT_APPROVED": "текст утверждён",
        "IMAGE_GENERATING": "создаётся изображение",
        "IMAGE_REVIEW": "проверка изображения",
        "READY_TO_PUBLISH": "готов к публикации",
        "PUBLISHING": "публикуется",
        "PUBLISHED": "опубликован",
        "WAITING_FOR_INPUT": "ожидает правки",
        "CANCELLED": "отменён",
        "FAILED_RECOVERABLE": "нужна повторная попытка",
        "PUBLISH_UNKNOWN": "результат публикации неизвестен",
    }
    lines = ["Последние черновики:"]
    for draft in drafts:
        label = state_labels.get(draft.state, draft.state.lower())
        lines.append(
            f"• {draft.draft_id} — {label}; текст v{draft.text_version}, "
            f"изображение v{draft.image_version}; {draft.updated_at}"
        )
    return "\n".join(lines)


def _render_settings(policy: BudgetPolicy) -> str:
    return (
        "Настройки Praeco:\n"
        "Язык постов: русский\n"
        f"Черновиков текста за запрос: {policy.text_drafts_per_request}\n"
        f"Изображений за запрос: {policy.image_outputs_per_request}\n"
        f"Автоповторов платных вызовов: {policy.automatic_paid_retry_limit}\n"
        f"Предупреждение на пост: ${policy.per_post_warning_usd:.2f}\n"
        f"Стоп на пост: ${policy.per_post_hard_limit_usd:.2f}\n"
        f"Стоп на месяц: ${policy.monthly_hard_limit_usd:.2f}\n"
        "Правила автора: /style"
    )


def _render_preferences(preferences: tuple[PreferenceRule, ...]) -> str:
    if not preferences:
        return "Постоянных правил автора пока нет.\n" + _style_usage()
    lines = ["Постоянные правила автора:"]
    for preference in preferences:
        scope = "текст" if preference.scope == "text" else "визуал"
        state = "вкл" if preference.active else "выкл"
        lines.append(
            f"#{preference.rule_id} · {scope} · v{preference.version} · "
            f"{state}: {preference.rule}"
        )
    lines.append(_style_usage())
    return "\n".join(lines)


def _style_usage() -> str:
    return (
        "Команды:\n"
        "/style text <правило>\n"
        "/style image <правило>\n"
        "/style off <ID>\n"
        "/style on <ID>"
    )


def _required_pending_image(
    pending: PendingImageInput | None,
) -> PendingImageInput:
    if pending is None:
        raise RuntimeError("pending image input is missing")
    return pending


def _queued_result(
    *,
    outcome: DispatchOutcome,
    draft_id: str,
    queue_outcome: QueueOutcome,
    job_id: int | None,
) -> DispatchResult:
    if job_id is None:
        raise RuntimeError("queued job has no persistent ID")
    return DispatchResult(
        outcome=outcome,
        draft_id=draft_id,
        job_id=job_id,
        budget_warning=queue_outcome is QueueOutcome.CREATED_WITH_WARNING,
    )
