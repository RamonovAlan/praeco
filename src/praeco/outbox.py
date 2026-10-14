"""Durable Telegram delivery outbox for immutable review versions."""

import hashlib
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from praeco.storage import immediate_transaction


class DeliveryError(RuntimeError):
    """Raised when a delivery transition or persisted payload is invalid."""


class DeliveryKind(StrEnum):
    """Telegram review payloads emitted after provider results are durable."""

    TEXT_REVIEW = "TEXT_REVIEW"
    IMAGE_REVIEW = "IMAGE_REVIEW"
    FINAL_REVIEW = "FINAL_REVIEW"
    PROGRESS_UPDATE = "PROGRESS_UPDATE"
    PROCESSING_ALERT = "PROCESSING_ALERT"
    PUBLICATION_RESULT = "PUBLICATION_RESULT"


class DeliveryOutcome(StrEnum):
    """Result of one delivery-worker iteration."""

    IDLE = "IDLE"
    SENT = "SENT"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class DeliveryRequest:
    """One immutable review version to deliver to an owner chat."""

    kind: DeliveryKind
    draft_id: str
    chat_id: int
    deduplication_key: str
    now: str
    text_version: int | None = None
    image_version: int | None = None
    job_id: int | None = None
    publication_id: int | None = None

    def __post_init__(self) -> None:
        for field_name in ("draft_id", "deduplication_key", "now"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} must not be blank")
        if self.chat_id == 0:
            raise ValueError("chat_id must not be zero")
        if self.kind is DeliveryKind.TEXT_REVIEW:
            if self.text_version is None or self.text_version <= 0:
                raise ValueError("text review requires a positive text_version")
            if self.image_version is not None:
                raise ValueError("text review must not include image_version")
            if self.job_id is not None:
                raise ValueError("text review must not include job_id")
            if self.publication_id is not None:
                raise ValueError("text review must not include publication_id")
        elif self.kind is DeliveryKind.IMAGE_REVIEW:
            if self.image_version is None or self.image_version <= 0:
                raise ValueError("image review requires a positive image_version")
            if self.text_version is not None:
                raise ValueError("image review must not include text_version")
            if self.job_id is not None:
                raise ValueError("image review must not include job_id")
            if self.publication_id is not None:
                raise ValueError("image review must not include publication_id")
        elif self.kind is DeliveryKind.FINAL_REVIEW:
            if self.text_version is None or self.text_version <= 0:
                raise ValueError("final review requires a positive text_version")
            if self.image_version is None or self.image_version <= 0:
                raise ValueError("final review requires a positive image_version")
            if self.job_id is not None:
                raise ValueError("final review must not include job_id")
            if self.publication_id is not None:
                raise ValueError("final review must not include publication_id")
        elif self.kind in {
            DeliveryKind.PROGRESS_UPDATE,
            DeliveryKind.PROCESSING_ALERT,
        }:
            if self.job_id is None or self.job_id <= 0:
                raise ValueError("status delivery requires a positive job_id")
            if self.text_version is not None or self.image_version is not None:
                raise ValueError("status delivery must not include versions")
            if self.publication_id is not None:
                raise ValueError("status delivery must not include publication_id")
        elif self.kind is DeliveryKind.PUBLICATION_RESULT:
            if self.publication_id is None or self.publication_id <= 0:
                raise ValueError("publication result requires a positive publication_id")
            if any(
                value is not None
                for value in (self.text_version, self.image_version, self.job_id)
            ):
                raise ValueError("publication result must not include version or job IDs")


@dataclass(frozen=True, slots=True)
class DeliveryLease:
    """One outbox item owned by a delivery worker until its lease expires."""

    delivery_id: int
    kind: DeliveryKind
    draft_id: str
    chat_id: int
    attempts: int
    text_version: int | None
    image_version: int | None
    job_id: int | None
    publication_id: int | None


@dataclass(frozen=True, slots=True)
class TelegramDelivery:
    """Exact private payload and callback actions passed to a Telegram adapter."""

    delivery_id: int
    kind: DeliveryKind
    chat_id: int
    draft_id: str
    approve_callback: str | None = None
    cancel_callback: str | None = None
    revision_callback: str | None = None
    manual_edit_callback: str | None = None
    owner_upload_callback: str | None = None
    return_to_text_callback: str | None = None
    body: str | None = field(default=None, repr=False)
    image_path: Path | None = field(default=None, repr=False)
    text_version: int | None = None
    image_version: int | None = None
    cost_usd: Decimal | None = None
    retry_callback: str | None = None
    publish_callback: str | None = None


@dataclass(frozen=True, slots=True)
class DeliveryRunResult:
    """Non-sensitive delivery result suitable for status reporting."""

    outcome: DeliveryOutcome
    delivery_id: int | None = None
    draft_id: str | None = None


class TelegramSender(Protocol):
    """Send one already-rendered review and return Telegram's message ID."""

    def send(self, delivery: TelegramDelivery) -> int: ...


class MockTelegramSender:
    """Record synthetic deliveries without contacting Telegram."""

    def __init__(self, *, message_id: int = 1) -> None:
        if message_id < 0:
            raise ValueError("message_id must not be negative")
        self._message_id = message_id
        self.calls = 0
        self.deliveries: list[TelegramDelivery] = []

    def send(self, delivery: TelegramDelivery) -> int:
        self.calls += 1
        self.deliveries.append(delivery)
        return self._message_id


class DeliveryRepository:
    """Persist and lease retryable Telegram sends without touching paid jobs."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        publishing_enabled: bool = False,
    ) -> None:
        self._connection = connection
        self._publishing_enabled = publishing_enabled

    def enqueue(self, request: DeliveryRequest) -> int:
        """Idempotently enqueue an immutable version and return its delivery ID."""

        with immediate_transaction(self._connection):
            self._connection.execute(
                """
                INSERT INTO telegram_outbox (
                    kind, draft_id, chat_id, text_version, image_version, job_id,
                    publication_id, state, next_attempt_at, deduplication_key,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?)
                ON CONFLICT (deduplication_key) DO NOTHING
                """,
                (
                    request.kind.value,
                    request.draft_id,
                    request.chat_id,
                    request.text_version,
                    request.image_version,
                    request.job_id,
                    request.publication_id,
                    request.now,
                    request.deduplication_key,
                    request.now,
                    request.now,
                ),
            )
            row = self._connection.execute(
                """
                SELECT id, kind, draft_id, chat_id, text_version, image_version,
                       job_id, publication_id
                FROM telegram_outbox WHERE deduplication_key = ?
                """,
                (request.deduplication_key,),
            ).fetchone()
            if row is None:
                raise DeliveryError("outbox insert did not persist")
            if not _row_matches_request(row=row, request=request):
                raise DeliveryError("deduplication key belongs to another delivery")
            return int(row["id"])

    def lease_next(
        self,
        *,
        worker_id: str,
        now: str,
        lease_expires_at: str,
    ) -> DeliveryLease | None:
        """Recover an expired lease and claim one ready delivery."""

        if not worker_id.strip():
            raise ValueError("worker_id must not be blank")
        if lease_expires_at <= now:
            raise ValueError("lease_expires_at must be later than now")
        with immediate_transaction(self._connection):
            self._connection.execute(
                """
                UPDATE telegram_outbox
                SET state = 'PENDING', lease_owner = NULL, lease_expires_at = NULL,
                    updated_at = ?
                WHERE state = 'LEASED' AND lease_expires_at <= ?
                """,
                (now, now),
            )
            row = self._connection.execute(
                """
                SELECT id, kind, draft_id, chat_id, text_version, image_version,
                       job_id, publication_id, attempts
                FROM telegram_outbox
                WHERE state = 'PENDING' AND next_attempt_at <= ?
                ORDER BY next_attempt_at, id
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                return None
            delivery_id = int(row["id"])
            cursor = self._connection.execute(
                """
                UPDATE telegram_outbox
                SET state = 'LEASED', attempts = attempts + 1, lease_owner = ?,
                    lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND state = 'PENDING'
                """,
                (worker_id, lease_expires_at, now, delivery_id),
            )
            if cursor.rowcount != 1:
                raise DeliveryError("ready delivery changed during lease transaction")
            return DeliveryLease(
                delivery_id=delivery_id,
                kind=DeliveryKind(str(row["kind"])),
                draft_id=str(row["draft_id"]),
                chat_id=int(row["chat_id"]),
                attempts=int(row["attempts"]) + 1,
                text_version=_optional_int(row["text_version"]),
                image_version=_optional_int(row["image_version"]),
                job_id=_optional_int(row["job_id"]),
                publication_id=_optional_int(row["publication_id"]),
            )

    def render(self, *, lease: DeliveryLease, media_directory: Path) -> TelegramDelivery:
        """Load the exact version named by a lease, never the draft's latest pointer."""

        if lease.kind is DeliveryKind.TEXT_REVIEW:
            if lease.text_version is None:
                raise DeliveryError("text delivery has no version")
            row = self._connection.execute(
                "SELECT body FROM text_versions WHERE draft_id = ? AND version = ?",
                (lease.draft_id, lease.text_version),
            ).fetchone()
            if row is None:
                raise DeliveryError("text delivery version is missing")
            return TelegramDelivery(
                delivery_id=lease.delivery_id,
                kind=lease.kind,
                chat_id=lease.chat_id,
                draft_id=lease.draft_id,
                body=str(row["body"]),
                approve_callback=f"txt_ok:{lease.draft_id}:{lease.text_version}",
                cancel_callback=f"txt_cancel:{lease.draft_id}:{lease.text_version}",
                revision_callback=f"txt_rev:{lease.draft_id}:{lease.text_version}",
                manual_edit_callback=f"txt_edit:{lease.draft_id}:{lease.text_version}",
            )

        if lease.kind is DeliveryKind.FINAL_REVIEW:
            return self._render_final(lease=lease, media_directory=media_directory)

        if lease.kind is DeliveryKind.PUBLICATION_RESULT:
            if lease.publication_id is None:
                raise DeliveryError("publication result has no publication ID")
            row = self._connection.execute(
                """
                SELECT state, error_category
                FROM publications WHERE id = ? AND draft_id = ?
                """,
                (lease.publication_id, lease.draft_id),
            ).fetchone()
            if row is None:
                raise DeliveryError("publication result is missing")
            state = str(row["state"])
            if state == "PUBLISHED":
                body = (
                    "LinkedIn подтвердил создание публикации. "
                    "Проверьте её отображение в своём профиле."
                )
            elif state == "PUBLISH_UNKNOWN":
                body = (
                    "LinkedIn мог создать публикацию, но подтверждение потеряно. "
                    "Автоматический повтор остановлен. Проверьте профиль вручную."
                )
            elif state == "FAILED":
                body = (
                    "LinkedIn отклонил публикацию до подтверждённого создания. "
                    "Черновик сохранён; повтор возможен только новой финальной кнопкой."
                )
            else:
                raise DeliveryError("publication result is not final")
            return TelegramDelivery(
                delivery_id=lease.delivery_id,
                kind=lease.kind,
                chat_id=lease.chat_id,
                draft_id=lease.draft_id,
                body=body,
            )

        if lease.kind is DeliveryKind.PROCESSING_ALERT:
            if lease.job_id is None:
                raise DeliveryError("processing alert has no job")
            row = self._connection.execute(
                """
                SELECT kind, state, error_category
                FROM jobs WHERE id = ? AND draft_id = ?
                """,
                (lease.job_id, lease.draft_id),
            ).fetchone()
            if (
                row is None
                or str(row["kind"]) != "generate_text"
                or str(row["state"]) != "RESULT_UNKNOWN"
            ):
                raise DeliveryError("processing alert job is not retryable")
            return TelegramDelivery(
                delivery_id=lease.delivery_id,
                kind=lease.kind,
                chat_id=lease.chat_id,
                draft_id=lease.draft_id,
                body=(
                    "Расшифровка готова, но черновик текста не был получен. "
                    "Автоматический повтор остановлен, чтобы не списать деньги дважды. "
                    "Нажмите кнопку, чтобы повторить только создание текста из уже "
                    "сохранённой расшифровки."
                ),
                retry_callback=f"txt_retry:{lease.draft_id}:{lease.job_id}",
            )

        if lease.kind is DeliveryKind.PROGRESS_UPDATE:
            if lease.job_id is None:
                raise DeliveryError("progress update has no job")
            row = self._connection.execute(
                """
                SELECT kind, state FROM jobs WHERE id = ? AND draft_id = ?
                """,
                (lease.job_id, lease.draft_id),
            ).fetchone()
            if (
                row is None
                or str(row["kind"]) != "transcribe_voice"
                or str(row["state"]) != "SUCCEEDED"
            ):
                raise DeliveryError("progress update job is invalid")
            return TelegramDelivery(
                delivery_id=lease.delivery_id,
                kind=lease.kind,
                chat_id=lease.chat_id,
                draft_id=lease.draft_id,
                body="Расшифровка готова. Готовлю черновик текста.",
            )

        if lease.image_version is None:
            raise DeliveryError("image delivery has no version")
        row = self._connection.execute(
            """
            SELECT local_path, sha256
            FROM image_versions WHERE draft_id = ? AND version = ?
            """,
            (lease.draft_id, lease.image_version),
        ).fetchone()
        if row is None:
            raise DeliveryError("image delivery version is missing")
        image_path = _validated_image_path(
            media_directory=media_directory,
            local_path=str(row["local_path"]),
            expected_sha256=str(row["sha256"]),
        )
        return TelegramDelivery(
            delivery_id=lease.delivery_id,
            kind=lease.kind,
            chat_id=lease.chat_id,
            draft_id=lease.draft_id,
            image_path=image_path,
            approve_callback=f"img_ok:{lease.draft_id}:{lease.image_version}",
            cancel_callback=f"img_cancel:{lease.draft_id}:{lease.image_version}",
            revision_callback=f"img_rev:{lease.draft_id}:{lease.image_version}",
            owner_upload_callback=(
                f"img_upload:{lease.draft_id}:{lease.image_version}"
            ),
            return_to_text_callback=(
                f"img_text:{lease.draft_id}:{lease.image_version}"
            ),
        )

    def _render_final(
        self,
        *,
        lease: DeliveryLease,
        media_directory: Path,
    ) -> TelegramDelivery:
        if lease.text_version is None or lease.image_version is None:
            raise DeliveryError("final delivery versions are missing")
        row = self._connection.execute(
            """
            SELECT t.body, i.local_path, i.sha256
            FROM text_versions AS t
            JOIN image_versions AS i
              ON i.draft_id = t.draft_id
             AND i.source_text_version = t.version
            WHERE t.draft_id = ? AND t.version = ? AND i.version = ?
              AND t.approved_at IS NOT NULL AND i.approved_at IS NOT NULL
            """,
            (lease.draft_id, lease.text_version, lease.image_version),
        ).fetchone()
        if row is None:
            raise DeliveryError("final delivery snapshot is missing or unapproved")
        image_path = _validated_image_path(
            media_directory=media_directory,
            local_path=str(row["local_path"]),
            expected_sha256=str(row["sha256"]),
        )
        cost_microusd = int(
            self._connection.execute(
                """
                SELECT COALESCE(
                    SUM(COALESCE(actual_cost_microusd, estimated_cost_microusd)),
                    0
                )
                FROM api_usage
                WHERE draft_id = ?
                """,
                (lease.draft_id,),
            ).fetchone()[0]
        )
        return TelegramDelivery(
            delivery_id=lease.delivery_id,
            kind=lease.kind,
            chat_id=lease.chat_id,
            draft_id=lease.draft_id,
            body=str(row["body"]),
            image_path=image_path,
            text_version=lease.text_version,
            image_version=lease.image_version,
            cost_usd=Decimal(cost_microusd) / Decimal(1_000_000),
            publish_callback=(
                f"pub:{lease.draft_id}:{lease.text_version}:{lease.image_version}"
                if self._publishing_enabled
                else None
            ),
        )

    def mark_sent(
        self,
        *,
        delivery_id: int,
        worker_id: str,
        telegram_message_id: int,
        now: str,
    ) -> None:
        if telegram_message_id < 0:
            raise ValueError("telegram_message_id must not be negative")
        with immediate_transaction(self._connection):
            cursor = self._connection.execute(
                """
                UPDATE telegram_outbox
                SET state = 'SENT', telegram_message_id = ?, sent_at = ?,
                    lease_owner = NULL, lease_expires_at = NULL, error_category = NULL,
                    updated_at = ?
                WHERE id = ? AND state = 'LEASED' AND lease_owner = ?
                """,
                (telegram_message_id, now, now, delivery_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise DeliveryError("delivery is not leased by this worker")

    def reschedule(
        self,
        *,
        delivery_id: int,
        worker_id: str,
        next_attempt_at: str,
        error_category: str,
        now: str,
    ) -> None:
        if next_attempt_at <= now:
            raise ValueError("next_attempt_at must be later than now")
        self._finish_attempt(
            delivery_id=delivery_id,
            worker_id=worker_id,
            state="PENDING",
            error_category=error_category,
            next_attempt_at=next_attempt_at,
            now=now,
        )

    def mark_failed(
        self,
        *,
        delivery_id: int,
        worker_id: str,
        error_category: str,
        now: str,
    ) -> None:
        self._finish_attempt(
            delivery_id=delivery_id,
            worker_id=worker_id,
            state="FAILED",
            error_category=error_category,
            next_attempt_at=now,
            now=now,
        )

    def _finish_attempt(
        self,
        *,
        delivery_id: int,
        worker_id: str,
        state: str,
        error_category: str,
        next_attempt_at: str,
        now: str,
    ) -> None:
        if not error_category.strip():
            raise ValueError("error_category must not be blank")
        with immediate_transaction(self._connection):
            cursor = self._connection.execute(
                """
                UPDATE telegram_outbox
                SET state = ?, next_attempt_at = ?, error_category = ?,
                    lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE id = ? AND state = 'LEASED' AND lease_owner = ?
                """,
                (state, next_attempt_at, error_category, now, delivery_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise DeliveryError("delivery is not leased by this worker")


class DeliveryWorker:
    """Send one durable review; retries never rerun an AI provider job."""

    def __init__(
        self,
        *,
        connection: sqlite3.Connection,
        media_directory: Path,
        sender: TelegramSender,
        max_attempts: int = 3,
        publishing_enabled: bool = False,
    ) -> None:
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self._repository = DeliveryRepository(
            connection,
            publishing_enabled=publishing_enabled,
        )
        self._media_directory = media_directory
        self._sender = sender
        self._max_attempts = max_attempts

    def run_once(
        self,
        *,
        worker_id: str,
        now: str,
        lease_expires_at: str,
        retry_at: str,
    ) -> DeliveryRunResult:
        lease = self._repository.lease_next(
            worker_id=worker_id,
            now=now,
            lease_expires_at=lease_expires_at,
        )
        if lease is None:
            return DeliveryRunResult(outcome=DeliveryOutcome.IDLE)
        try:
            delivery = self._repository.render(
                lease=lease,
                media_directory=self._media_directory,
            )
        except DeliveryError:
            self._repository.mark_failed(
                delivery_id=lease.delivery_id,
                worker_id=worker_id,
                error_category="invalid_persisted_payload",
                now=now,
            )
            return DeliveryRunResult(
                outcome=DeliveryOutcome.FAILED,
                delivery_id=lease.delivery_id,
                draft_id=lease.draft_id,
            )
        try:
            telegram_message_id = self._sender.send(delivery)
        except Exception:
            if lease.attempts >= self._max_attempts:
                self._repository.mark_failed(
                    delivery_id=lease.delivery_id,
                    worker_id=worker_id,
                    error_category="telegram_send_failed",
                    now=now,
                )
                outcome = DeliveryOutcome.FAILED
            else:
                self._repository.reschedule(
                    delivery_id=lease.delivery_id,
                    worker_id=worker_id,
                    next_attempt_at=retry_at,
                    error_category="telegram_send_failed",
                    now=now,
                )
                outcome = DeliveryOutcome.RETRY_SCHEDULED
            return DeliveryRunResult(
                outcome=outcome,
                delivery_id=lease.delivery_id,
                draft_id=lease.draft_id,
            )
        self._repository.mark_sent(
            delivery_id=lease.delivery_id,
            worker_id=worker_id,
            telegram_message_id=telegram_message_id,
            now=now,
        )
        return DeliveryRunResult(
            outcome=DeliveryOutcome.SENT,
            delivery_id=lease.delivery_id,
            draft_id=lease.draft_id,
        )


def _row_matches_request(*, row: sqlite3.Row, request: DeliveryRequest) -> bool:
    return (
        str(row["kind"]) == request.kind.value
        and str(row["draft_id"]) == request.draft_id
        and int(row["chat_id"]) == request.chat_id
        and _optional_int(row["text_version"]) == request.text_version
        and _optional_int(row["image_version"]) == request.image_version
        and _optional_int(row["job_id"]) == request.job_id
        and _optional_int(row["publication_id"]) == request.publication_id
    )


def _validated_image_path(
    *,
    media_directory: Path,
    local_path: str,
    expected_sha256: str,
) -> Path:
    media_root = media_directory.resolve()
    image_path = (media_root / local_path).resolve()
    if not image_path.is_relative_to(media_root):
        raise DeliveryError("image delivery path escapes media directory")
    if not image_path.is_file():
        raise DeliveryError("image delivery file is missing")
    with image_path.open("rb") as image_file:
        digest = hashlib.file_digest(image_file, "sha256").hexdigest()
    if digest != expected_sha256:
        raise DeliveryError("image delivery checksum does not match")
    return image_path


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    raise TypeError("SQLite version pointer must be an integer or null")
