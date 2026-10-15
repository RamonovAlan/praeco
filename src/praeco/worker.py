"""Single-worker execution that persists provider results before delivery."""

# ruff: noqa: RUF001 -- Russian owner revision markers are intentional.

import hashlib
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from praeco.budget import BudgetPolicy
from praeco.drafts import DraftRepository
from praeco.editing import (
    ImageEditingRepository,
    ImageInputMode,
    TextEditingRepository,
    TextInputMode,
    VoiceEditingRepository,
    VoiceRevisionInput,
    VoiceRevisionTarget,
)
from praeco.jobs import (
    JobLease,
    JobRepository,
    PaidJobRequest,
    QueueOutcome,
    UsageMetadata,
)
from praeco.outbox import DeliveryKind, DeliveryRepository, DeliveryRequest
from praeco.owner import OwnerRepository
from praeco.providers import (
    ImageGenerationRequest,
    ImageGenerationResult,
    ImageProvider,
    ProviderCallError,
    TextGenerationRequest,
    TextGenerationResult,
    TextProvider,
    TranscriptionProvider,
    TranscriptionRequest,
    TranscriptionResult,
)
from praeco.storage import immediate_transaction

_TEXT_PROMPT_VERSION = "text-v0.3"
_STYLE_PROFILE_VERSION = "style-v2"


class WorkerInputError(RuntimeError):
    """Raised when durable job input is missing before a provider is contacted."""


class WorkerOutcome(StrEnum):
    """Persisted outcome of one worker iteration."""

    IDLE = "IDLE"
    TEXT_STORED = "TEXT_STORED"
    TRANSCRIPT_STORED = "TRANSCRIPT_STORED"
    TRANSCRIPT_STORED_BUDGET_BLOCKED = "TRANSCRIPT_STORED_BUDGET_BLOCKED"
    REVISION_TRANSCRIPT_STORED = "REVISION_TRANSCRIPT_STORED"
    IMAGE_STORED = "IMAGE_STORED"
    FAILED_BEFORE_CALL = "FAILED_BEFORE_CALL"
    FAILED_AFTER_CALL = "FAILED_AFTER_CALL"


@dataclass(frozen=True, slots=True)
class WorkerResult:
    """Non-sensitive result for later runtime status reporting."""

    outcome: WorkerOutcome
    job_id: int | None = None
    draft_id: str | None = None
    next_job_id: int | None = None


class SingleWorker:
    """Execute one leased job through injected providers; never deliver messages."""

    def __init__(
        self,
        *,
        connection: sqlite3.Connection,
        owner_id: int,
        media_directory: Path,
        text_provider: TextProvider,
        transcription_provider: TranscriptionProvider,
        image_provider: ImageProvider,
        budget_policy: BudgetPolicy,
        next_text_estimate_usd: Decimal,
    ) -> None:
        if owner_id <= 0:
            raise ValueError("owner_id must be positive")
        if next_text_estimate_usd < 0:
            raise ValueError("next_text_estimate_usd must not be negative")
        self._connection = connection
        self._owner_id = owner_id
        self._media_directory = media_directory
        self._text_provider = text_provider
        self._transcription_provider = transcription_provider
        self._image_provider = image_provider
        self._budget_policy = budget_policy
        self._next_text_estimate_usd = next_text_estimate_usd
        self._jobs = JobRepository(connection)
        self._drafts = DraftRepository(connection)
        self._editing = TextEditingRepository(connection)
        self._image_editing = ImageEditingRepository(connection)
        self._voice_editing = VoiceEditingRepository(connection)
        self._deliveries = DeliveryRepository(connection)
        self._owner = OwnerRepository(connection)

    def run_once(
        self,
        *,
        worker_id: str,
        now: str,
        lease_expires_at: str,
    ) -> WorkerResult:
        """Lease and execute at most one job, persisting before any future delivery."""

        lease = self._jobs.lease_next(
            worker_id=worker_id,
            now=now,
            lease_expires_at=lease_expires_at,
        )
        if lease is None:
            return WorkerResult(outcome=WorkerOutcome.IDLE)
        try:
            if lease.kind == "generate_text":
                return self._run_text(lease=lease, worker_id=worker_id, now=now)
            if lease.kind == "transcribe_voice":
                return self._run_transcription(
                    lease=lease,
                    worker_id=worker_id,
                    now=now,
                )
            if lease.kind == "transcribe_revision":
                return self._run_revision_transcription(
                    lease=lease,
                    worker_id=worker_id,
                    now=now,
                )
            if lease.kind == "generate_image":
                return self._run_image(lease=lease, worker_id=worker_id, now=now)
            raise WorkerInputError("unsupported job kind")
        except WorkerInputError:
            if lease.kind == "transcribe_revision":
                context = self._voice_editing.get(
                    transcription_job_id=lease.job_id
                )
                if context is not None:
                    self._jobs.cancel_queued(
                        job_id=context.generation_job_id,
                        error_category="transcription_prerequisite_failed",
                        now=now,
                    )
            self._jobs.fail_before_external_call(
                job_id=lease.job_id,
                worker_id=worker_id,
                error_category="invalid_local_input",
                now=now,
            )
            return WorkerResult(
                outcome=WorkerOutcome.FAILED_BEFORE_CALL,
                job_id=lease.job_id,
                draft_id=lease.draft_id,
            )

    def _run_revision_transcription(
        self,
        *,
        lease: JobLease,
        worker_id: str,
        now: str,
    ) -> WorkerResult:
        draft_id = _required_draft_id(lease)
        context = self._voice_editing.get(transcription_job_id=lease.job_id)
        if context is None or context.draft_id != draft_id:
            raise WorkerInputError("voice revision context is missing or stale")
        self._validate_revision_binding(context)
        audio_path = staged_voice_path(
            media_directory=self._media_directory,
            draft_id=f"tg-{context.telegram_update_id}",
        )
        if not audio_path.is_file():
            raise WorkerInputError("staged revision voice file is missing")
        request = TranscriptionRequest(
            audio_path=audio_path,
            mime_type=context.mime_type,
            duration_seconds=Decimal(context.duration_seconds),
            language_hint=None,
        )
        self._jobs.mark_external_call_started(
            job_id=lease.job_id,
            worker_id=worker_id,
            now=now,
        )
        try:
            result = self._transcription_provider.transcribe(request)
            self._persist_revision_transcription(
                lease=lease,
                worker_id=worker_id,
                context=context,
                result=result,
                now=now,
            )
        except BaseException:
            self._jobs.cancel_queued(
                job_id=context.generation_job_id,
                error_category="transcription_prerequisite_unknown",
                now=now,
            )
            raise
        return WorkerResult(
            outcome=WorkerOutcome.REVISION_TRANSCRIPT_STORED,
            job_id=lease.job_id,
            draft_id=draft_id,
            next_job_id=context.generation_job_id,
        )

    def _persist_revision_transcription(
        self,
        *,
        lease: JobLease,
        worker_id: str,
        context: VoiceRevisionInput,
        result: TranscriptionResult,
        now: str,
    ) -> None:
        with immediate_transaction(self._connection):
            if context.target is VoiceRevisionTarget.TEXT:
                pending = self._editing.get_pending(owner_id=context.owner_id)
                if (
                    pending is None
                    or pending.draft_id != context.draft_id
                    or pending.base_text_version != context.base_text_version
                    or pending.mode is not TextInputMode.AI_REVISION
                    or pending.prompt_message_id != context.prompt_message_id
                ):
                    raise RuntimeError("bound text revision changed after provider call")
                self._editing.record_revision_request(
                    job_id=context.generation_job_id,
                    pending=pending,
                    comment=result.transcript,
                    source_kind="voice",
                    now=now,
                )
                self._drafts.start_text_generation(
                    draft_id=context.draft_id,
                    actor_id=context.owner_id,
                    now=now,
                )
                self._editing.consume(pending=pending)
            else:
                pending_image = self._image_editing.get_pending(
                    owner_id=context.owner_id
                )
                if (
                    pending_image is None
                    or context.base_image_version is None
                    or pending_image.draft_id != context.draft_id
                    or pending_image.base_image_version != context.base_image_version
                    or pending_image.source_text_version != context.base_text_version
                    or pending_image.mode is not ImageInputMode.AI_REVISION
                    or pending_image.prompt_message_id != context.prompt_message_id
                ):
                    raise RuntimeError("bound image revision changed after provider call")
                self._image_editing.record_revision_request(
                    job_id=context.generation_job_id,
                    pending=pending_image,
                    comment=result.transcript,
                    now=now,
                )
                self._drafts.start_image_generation(
                    draft_id=context.draft_id,
                    actor_id=context.owner_id,
                    now=now,
                )
                self._image_editing.consume(pending=pending_image)
            self._jobs.finalize_succeeded(
                job_id=lease.job_id,
                worker_id=worker_id,
                usage=result.metering.usage_metadata(
                    stage="revision_transcription",
                    now=now,
                ),
                actual_cost_usd=result.metering.actual_cost_usd,
            )

    def _validate_revision_binding(self, context: VoiceRevisionInput) -> None:
        if context.target is VoiceRevisionTarget.TEXT:
            pending = self._editing.get_pending(owner_id=context.owner_id)
            if (
                pending is None
                or pending.draft_id != context.draft_id
                or pending.base_text_version != context.base_text_version
                or pending.mode is not TextInputMode.AI_REVISION
                or pending.prompt_message_id != context.prompt_message_id
            ):
                raise WorkerInputError("bound text revision is missing or stale")
            return
        pending_image = self._image_editing.get_pending(owner_id=context.owner_id)
        if (
            pending_image is None
            or context.base_image_version is None
            or pending_image.draft_id != context.draft_id
            or pending_image.base_image_version != context.base_image_version
            or pending_image.source_text_version != context.base_text_version
            or pending_image.mode is not ImageInputMode.AI_REVISION
            or pending_image.prompt_message_id != context.prompt_message_id
        ):
            raise WorkerInputError("bound image revision is missing or stale")

    def _run_text(self, *, lease: JobLease, worker_id: str, now: str) -> WorkerResult:
        draft_id = _required_draft_id(lease)
        source = self._drafts.get_source(draft_id)
        current = (
            self._drafts.get_text_version(
                draft_id=draft_id,
                version=lease.input_version,
            )
            if lease.input_version > 0
            else None
        )
        revision = self._editing.get_revision_request(job_id=lease.job_id)
        if lease.input_version > 0 and (
            revision is None
            or revision.draft_id != draft_id
            or revision.base_text_version != lease.input_version
        ):
            raise WorkerInputError("text revision context is missing or stale")
        style_rules = self._owner.active_rule_texts(scope="text")
        request = TextGenerationRequest(
            source_text=source.source_text,
            language=source.language,
            current_text=current.body if current else None,
            revision_comment=revision.comment if revision else None,
            style_rules=style_rules,
        )
        self._jobs.mark_external_call_started(
            job_id=lease.job_id,
            worker_id=worker_id,
            now=now,
        )
        try:
            result = self._text_provider.generate_text(request)
        except ProviderCallError as error:
            self._persist_text_provider_failure(
                lease=lease,
                worker_id=worker_id,
                error=error,
                now=now,
            )
            return WorkerResult(
                outcome=WorkerOutcome.FAILED_AFTER_CALL,
                job_id=lease.job_id,
                draft_id=draft_id,
            )
        self._persist_text(
            lease=lease,
            worker_id=worker_id,
            result=result,
            revision_comment=revision.comment if revision else None,
            style_version=_style_version(style_rules),
            now=now,
        )
        return WorkerResult(
            outcome=WorkerOutcome.TEXT_STORED,
            job_id=lease.job_id,
            draft_id=draft_id,
        )

    def _persist_text_provider_failure(
        self,
        *,
        lease: JobLease,
        worker_id: str,
        error: ProviderCallError,
        now: str,
    ) -> None:
        draft_id = _required_draft_id(lease)
        usage = (
            error.metering.usage_metadata(stage="text", now=now)
            if error.metering is not None
            else UsageMetadata(
                stage="text",
                provider_model="provider-unknown",
                price_version="unknown",
                now=now,
            )
        )
        with immediate_transaction(self._connection):
            self._jobs.finalize_unknown_billed(
                job_id=lease.job_id,
                worker_id=worker_id,
                error_category=error.category,
                usage=usage,
            )
            self._drafts.mark_generation_failed(draft_id=draft_id, now=now)
            self._deliveries.enqueue(
                DeliveryRequest(
                    kind=DeliveryKind.PROCESSING_ALERT,
                    draft_id=draft_id,
                    chat_id=self._owner_id,
                    job_id=lease.job_id,
                    deduplication_key=f"telegram:processing-alert:{lease.job_id}",
                    now=now,
                )
            )

    def _persist_text(
        self,
        *,
        lease: JobLease,
        worker_id: str,
        result: TextGenerationResult,
        revision_comment: str | None,
        style_version: str,
        now: str,
    ) -> None:
        draft_id = _required_draft_id(lease)
        workflow = self._drafts.get(draft_id)
        text_version = workflow.current_text_version + 1
        with immediate_transaction(self._connection):
            self._drafts.record_text(
                draft_id=draft_id,
                actor_id=self._owner_id,
                version=text_version,
                body=result.body,
                revision_comment=revision_comment,
                prompt_version=_TEXT_PROMPT_VERSION,
                style_version=style_version,
                provider_model=result.metering.provider_model,
                image_brief=result.image_brief,
                now=now,
            )
            self._jobs.finalize_succeeded(
                job_id=lease.job_id,
                worker_id=worker_id,
                usage=result.metering.usage_metadata(
                    stage="text_generation",
                    now=now,
                ),
                actual_cost_usd=result.metering.actual_cost_usd,
            )
            self._deliveries.enqueue(
                DeliveryRequest(
                    kind=DeliveryKind.TEXT_REVIEW,
                    draft_id=draft_id,
                    chat_id=self._owner_id,
                    text_version=text_version,
                    deduplication_key=(
                        f"telegram:text-review:{draft_id}:{text_version}"
                    ),
                    now=now,
                )
            )

    def _run_transcription(
        self,
        *,
        lease: JobLease,
        worker_id: str,
        now: str,
    ) -> WorkerResult:
        draft_id = _required_draft_id(lease)
        source = self._drafts.get_source(draft_id)
        if source.voice_source is None:
            raise WorkerInputError("voice metadata is missing")
        audio_path = staged_voice_path(
            media_directory=self._media_directory,
            draft_id=draft_id,
        )
        if not audio_path.is_file():
            raise WorkerInputError("staged voice file is missing")
        request = TranscriptionRequest(
            audio_path=audio_path,
            mime_type=source.voice_source.mime_type or "audio/ogg",
            duration_seconds=Decimal(source.voice_source.duration_seconds),
            language_hint=None if source.language == "und" else source.language,
        )
        self._jobs.mark_external_call_started(
            job_id=lease.job_id,
            worker_id=worker_id,
            now=now,
        )
        result = self._transcription_provider.transcribe(request)
        next_job_id, blocked = self._persist_transcription(
            lease=lease,
            worker_id=worker_id,
            result=result,
            now=now,
        )
        return WorkerResult(
            outcome=(
                WorkerOutcome.TRANSCRIPT_STORED_BUDGET_BLOCKED
                if blocked
                else WorkerOutcome.TRANSCRIPT_STORED
            ),
            job_id=lease.job_id,
            draft_id=draft_id,
            next_job_id=next_job_id,
        )

    def _persist_transcription(
        self,
        *,
        lease: JobLease,
        worker_id: str,
        result: TranscriptionResult,
        now: str,
    ) -> tuple[int | None, bool]:
        draft_id = _required_draft_id(lease)
        billing_month = _billing_month(now)
        with immediate_transaction(self._connection):
            self._drafts.record_transcript(
                draft_id=draft_id,
                actor_id=self._owner_id,
                transcript=result.transcript,
                language=result.language,
                now=now,
            )
            self._jobs.finalize_succeeded(
                job_id=lease.job_id,
                worker_id=worker_id,
                usage=result.metering.usage_metadata(
                    stage="transcription",
                    now=now,
                ),
                actual_cost_usd=result.metering.actual_cost_usd,
            )
            queued = self._jobs.enqueue_paid(
                request=PaidJobRequest(
                    kind="generate_text",
                    draft_id=draft_id,
                    input_version=0,
                    deduplication_key=f"text:{draft_id}:transcript",
                    estimated_cost_usd=self._next_text_estimate_usd,
                    billing_month=billing_month,
                    now=now,
                ),
                policy=self._budget_policy,
            )
            if queued.outcome is QueueOutcome.BUDGET_BLOCKED:
                return None, True
            self._drafts.start_text_generation(
                draft_id=draft_id,
                actor_id=self._owner_id,
                now=now,
            )
            self._deliveries.enqueue(
                DeliveryRequest(
                    kind=DeliveryKind.PROGRESS_UPDATE,
                    draft_id=draft_id,
                    chat_id=self._owner_id,
                    job_id=lease.job_id,
                    deduplication_key=f"telegram:transcription-ready:{lease.job_id}",
                    now=now,
                )
            )
            return queued.job_id, False

    def _run_image(self, *, lease: JobLease, worker_id: str, now: str) -> WorkerResult:
        draft_id = _required_draft_id(lease)
        text = self._drafts.get_text_version(
            draft_id=draft_id,
            version=lease.input_version,
        )
        if text.image_brief is None:
            raise RuntimeError("approved text image brief changed after provider call")
        revision = self._image_editing.get_revision_request(job_id=lease.job_id)
        source_image_path = None
        prompt = f"Existing visual brief:\n{text.image_brief}"
        width = 1536
        height = 1024
        if revision is not None:
            if (
                revision.draft_id != draft_id
                or revision.source_text_version != lease.input_version
            ):
                raise WorkerInputError("image revision context is stale")
            source = self._drafts.get_image_version(
                draft_id=draft_id,
                version=revision.base_image_version,
            )
            if source.source_text_version != lease.input_version:
                raise WorkerInputError("source image belongs to another text version")
            fresh_concept = _requires_fresh_image_concept(revision.comment)
            if not fresh_concept:
                source_image_path = self._verified_image_path(
                    local_path=source.local_path,
                    expected_sha256=source.sha256,
                )
            prompt += f"\n\nOwner revision request:\n{revision.comment}"
            if fresh_concept:
                prompt += (
                    "\n\nThis is a concept reset. Generate from scratch. Do not reuse "
                    "the previous subject, metaphor, objects, composition, geometry, "
                    "layout, or camera angle. Choose a clearly different semantic angle "
                    "from the approved post."
                )
            width = source.width
            height = source.height
        image_rules = self._owner.active_rule_texts(scope="image")
        if image_rules:
            prompt += "\nActive owner visual rules:\n" + "\n".join(
                f"- {rule}" for rule in image_rules
            )
        request = ImageGenerationRequest(
            brief=text.image_brief,
            approved_text=text.body,
            prompt=prompt,
            width=width,
            height=height,
            source_image_path=source_image_path,
        )
        self._jobs.mark_external_call_started(
            job_id=lease.job_id,
            worker_id=worker_id,
            now=now,
        )
        result = self._image_provider.generate_image(request)
        relative_path, absolute_path = self._write_image(
            draft_id=draft_id,
            result=result,
        )
        try:
            self._persist_image(
                lease=lease,
                worker_id=worker_id,
                result=result,
                relative_path=relative_path,
                prompt=prompt,
                now=now,
            )
        except BaseException:
            absolute_path.unlink(missing_ok=True)
            raise
        return WorkerResult(
            outcome=WorkerOutcome.IMAGE_STORED,
            job_id=lease.job_id,
            draft_id=draft_id,
        )

    def _write_image(
        self,
        *,
        draft_id: str,
        result: ImageGenerationResult,
    ) -> tuple[Path, Path]:
        extension = _image_extension(result.mime_type)
        workflow = self._drafts.get(draft_id)
        relative_path = Path(_safe_id(draft_id)) / (
            f"image-v{workflow.current_image_version + 1}{extension}"
        )
        absolute_path = self._media_directory / relative_path
        absolute_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with absolute_path.open("xb") as image_file:
            image_file.write(result.content)
        absolute_path.chmod(0o600)
        return relative_path, absolute_path

    def _persist_image(
        self,
        *,
        lease: JobLease,
        worker_id: str,
        result: ImageGenerationResult,
        relative_path: Path,
        prompt: str,
        now: str,
    ) -> None:
        draft_id = _required_draft_id(lease)
        workflow = self._drafts.get(draft_id)
        image_version = workflow.current_image_version + 1
        text = self._drafts.get_text_version(
            draft_id=draft_id,
            version=lease.input_version,
        )
        if text.image_brief is None:
            raise WorkerInputError("approved text has no image brief")
        with immediate_transaction(self._connection):
            self._drafts.record_image(
                draft_id=draft_id,
                actor_id=self._owner_id,
                version=image_version,
                source_text_version=lease.input_version,
                brief=text.image_brief,
                prompt=prompt,
                local_path=str(relative_path),
                sha256=hashlib.sha256(result.content).hexdigest(),
                mime_type=result.mime_type,
                width=result.width,
                height=result.height,
                now=now,
            )
            self._jobs.finalize_succeeded(
                job_id=lease.job_id,
                worker_id=worker_id,
                usage=result.metering.usage_metadata(
                    stage="image_generation",
                    now=now,
                ),
                actual_cost_usd=result.metering.actual_cost_usd,
            )
            self._deliveries.enqueue(
                DeliveryRequest(
                    kind=DeliveryKind.IMAGE_REVIEW,
                    draft_id=draft_id,
                    chat_id=self._owner_id,
                    image_version=image_version,
                    deduplication_key=(
                        f"telegram:image-review:{draft_id}:{image_version}"
                    ),
                    now=now,
                )
            )

    def _verified_image_path(
        self,
        *,
        local_path: str,
        expected_sha256: str,
    ) -> Path:
        media_root = self._media_directory.resolve()
        image_path = (media_root / local_path).resolve()
        if not image_path.is_relative_to(media_root) or not image_path.is_file():
            raise WorkerInputError("source image path is invalid")
        with image_path.open("rb") as image_file:
            digest = hashlib.file_digest(image_file, "sha256").hexdigest()
        if digest != expected_sha256:
            raise WorkerInputError("source image checksum does not match")
        return image_path


def recover_orphaned_text_calls(
    *,
    connection: sqlite3.Connection,
    owner_id: int,
    now: str,
) -> int:
    """Expose post-boundary text calls left ambiguous by an earlier process exit."""

    if owner_id <= 0:
        raise ValueError("owner_id must be positive")
    rows = connection.execute(
        """
        SELECT j.id, j.draft_id, j.lease_owner
        FROM jobs AS j
        JOIN drafts AS d ON d.id = j.draft_id
        WHERE j.kind = 'generate_text'
          AND j.state = 'RESULT_UNKNOWN'
          AND j.lease_owner IS NOT NULL
          AND d.state = 'TEXT_GENERATING'
          AND NOT EXISTS (SELECT 1 FROM api_usage AS u WHERE u.job_id = j.id)
        ORDER BY j.id
        """
    ).fetchall()
    recovered = 0
    jobs = JobRepository(connection)
    drafts = DraftRepository(connection)
    deliveries = DeliveryRepository(connection)
    for row in rows:
        job_id = int(row["id"])
        draft_id = str(row["draft_id"])
        worker_id = str(row["lease_owner"])
        with immediate_transaction(connection):
            jobs.finalize_unknown_billed(
                job_id=job_id,
                worker_id=worker_id,
                error_category="process_exit_after_call",
                usage=UsageMetadata(
                    stage="text",
                    provider_model="provider-unknown",
                    price_version="unknown",
                    now=now,
                ),
            )
            drafts.mark_generation_failed(draft_id=draft_id, now=now)
            deliveries.enqueue(
                DeliveryRequest(
                    kind=DeliveryKind.PROCESSING_ALERT,
                    draft_id=draft_id,
                    chat_id=owner_id,
                    job_id=job_id,
                    deduplication_key=f"telegram:processing-alert:{job_id}",
                    now=now,
                )
            )
        recovered += 1
    return recovered


def _required_draft_id(lease: JobLease) -> str:
    if lease.draft_id is None:
        raise WorkerInputError("job has no draft")
    return lease.draft_id


def _requires_fresh_image_concept(comment: str) -> bool:
    normalized = " ".join(comment.casefold().replace("ё", "е").split())
    markers = (
        "другая концепц",
        "другой концепт",
        "измени концепц",
        "измени концепт",
        "изменить концепц",
        "изменить концепт",
        "новая концепц",
        "новый концепт",
        "совсем другая",
        "совсем другой",
        "полностью измен",
        "полностью помен",
        "с нуля",
        "заново",
        "попробуй иначе",
        "не относится к текст",
        "не понятно как это к текст",
        "непонятно как это к текст",
        "перечитай",
        "different concept",
        "new concept",
        "change the concept",
        "start over",
        "from scratch",
        "completely change",
    )
    return any(marker in normalized for marker in markers)


def _style_version(style_rules: tuple[str, ...]) -> str:
    if not style_rules:
        return _STYLE_PROFILE_VERSION
    digest = hashlib.sha256("\n".join(style_rules).encode("utf-8")).hexdigest()[:12]
    return f"{_STYLE_PROFILE_VERSION}+owner-{digest}"


def _safe_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def staged_voice_path(*, media_directory: Path, draft_id: str) -> Path:
    """Return the safe local path where the Telegram downloader stages one voice."""

    return media_directory / "incoming" / f"{_safe_id(draft_id)}.ogg"


def _image_extension(mime_type: str) -> str:
    extensions = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
    }
    try:
        return extensions[mime_type]
    except KeyError as error:
        raise RuntimeError("provider returned an unsupported image MIME type") from error


def _billing_month(now: str) -> str:
    if len(now) < 7 or now[4] != "-":
        raise WorkerInputError("worker timestamp must start with YYYY-MM")
    return now[:7]
