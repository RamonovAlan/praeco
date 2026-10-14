"""Transactional Telegram-to-domain dispatch without network or provider calls."""

import hashlib
import re
import sqlite3
from dataclasses import dataclass
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
    r"^(?P<action>txt_ok|txt_rev|txt_edit|txt_cancel|txt_retry|img_ok|img_rev|"
    r"img_upload|img_text|img_cancel|pub):"
    r"(?P<draft_id>[A-Za-z0-9_-]{1,40}):(?P<version>[0-9]{1,9})"
    r"(?::(?P<image_version>[0-9]{1,9}))?$"
)


class DispatchOutcome(StrEnum):
    """Small result vocabulary consumed later by the Telegram delivery adapter."""

    WELCOME = "WELCOME"
    HELP = "HELP"
    STATUS = "STATUS"
    LINKEDIN_CONNECT = "LINKEDIN_CONNECT"
    WAITING_FOR_INPUT = "WAITING_FOR_INPUT"
    TEXT_JOB_QUEUED = "TEXT_JOB_QUEUED"
    TEXT_REVISION_REQUESTED = "TEXT_REVISION_REQUESTED"
    MANUAL_EDIT_REQUESTED = "MANUAL_EDIT_REQUESTED"
    TEXT_REVISION_JOB_QUEUED = "TEXT_REVISION_JOB_QUEUED"
    TEXT_MANUALLY_EDITED = "TEXT_MANUALLY_EDITED"
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
                    return DispatchResult(outcome=DispatchOutcome.DUPLICATE)
                if isinstance(event, TelegramTextInput):
                    return self._dispatch_text(
                        event=event,
                        now=now,
                        billing_month=billing_month,
                    )
                if isinstance(event, TelegramVoiceInput):
                    return self._dispatch_voice(
                        event=event,
                        now=now,
                        billing_month=billing_month,
                    )
                if isinstance(event, TelegramImageInput):
                    return self._dispatch_image(event=event, now=now)
                if callback is None:
                    raise RuntimeError("parsed callback action is missing")
                return self._dispatch_callback(
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

    def _dispatch_text(
        self,
        *,
        event: TelegramTextInput,
        now: str,
        billing_month: str,
    ) -> DispatchResult:
        if event.command is not None:
            if event.command == "/new":
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
            base_image = self._drafts.get_image_version(
                draft_id=pending.draft_id,
                version=pending.base_image_version,
            )
            self._drafts.record_owner_image(
                draft_id=pending.draft_id,
                actor_id=event.actor_id,
                version=version,
                source_text_version=pending.source_text_version,
                brief=base_image.brief,
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
