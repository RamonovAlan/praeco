"""Durable owner input bindings and version-bound revision requests."""

import sqlite3
from dataclasses import dataclass, field
from enum import StrEnum

from praeco.storage import immediate_transaction


class EditingError(RuntimeError):
    """Raised when a pending input binding changes unexpectedly."""


class PendingInputConflict(EditingError):
    """Raised when another draft is already waiting for owner input."""


class TextInputMode(StrEnum):
    """How the next owner message must be applied to a bound text version."""

    AI_REVISION = "AI_REVISION"
    MANUAL_EDIT = "MANUAL_EDIT"


class ImageInputMode(StrEnum):
    """How the next owner message must be applied to a bound image version."""

    AI_REVISION = "AI_REVISION"
    OWNER_UPLOAD = "OWNER_UPLOAD"


class VoiceRevisionTarget(StrEnum):
    """Versioned revision flow that owns one voice transcription."""

    TEXT = "TEXT"
    IMAGE = "IMAGE"


@dataclass(frozen=True, slots=True)
class PendingTextInput:
    """One explicit binding from a Telegram button to the next owner response."""

    owner_id: int
    draft_id: str
    base_text_version: int
    mode: TextInputMode
    prompt_message_id: int


@dataclass(frozen=True, slots=True)
class TextRevisionRequest:
    """Private revision instruction attached to one paid text job."""

    job_id: int
    draft_id: str
    base_text_version: int
    comment: str = field(repr=False)
    source_kind: str


@dataclass(frozen=True, slots=True)
class PendingImageInput:
    """One explicit binding from an image-review button to the next response."""

    owner_id: int
    draft_id: str
    base_image_version: int
    source_text_version: int
    mode: ImageInputMode
    prompt_message_id: int


@dataclass(frozen=True, slots=True)
class ImageRevisionRequest:
    """Private revision instruction attached to one paid image job."""

    job_id: int
    draft_id: str
    base_image_version: int
    source_text_version: int
    comment: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class VoiceRevisionInput:
    """Durable bridge from one voice upload to one dependent revision job."""

    transcription_job_id: int
    generation_job_id: int
    owner_id: int
    draft_id: str
    target: VoiceRevisionTarget
    base_text_version: int
    base_image_version: int | None
    prompt_message_id: int
    telegram_update_id: int
    file_unique_id: str
    duration_seconds: int
    file_size: int | None
    mime_type: str


class TextEditingRepository:
    """Persist explicit edit context without exposing private revision text."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def bind(
        self,
        *,
        owner_id: int,
        draft_id: str,
        base_text_version: int,
        mode: TextInputMode,
        prompt_message_id: int,
        now: str,
    ) -> PendingTextInput:
        """Bind the next response, rejecting ambiguous simultaneous bindings."""

        if owner_id <= 0 or base_text_version <= 0 or prompt_message_id < 0:
            raise ValueError("editing binding identifiers are invalid")
        if not draft_id.strip() or not now.strip():
            raise ValueError("draft_id and now must not be blank")
        with immediate_transaction(self._connection):
            existing = self.get_pending(owner_id=owner_id)
            if existing is not None or _has_pending_image(
                self._connection, owner_id=owner_id
            ):
                raise PendingInputConflict("another owner input is already pending")
            self._connection.execute(
                """
                INSERT INTO pending_text_inputs (
                    owner_id, draft_id, base_text_version, mode,
                    prompt_message_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    owner_id,
                    draft_id,
                    base_text_version,
                    mode.value,
                    prompt_message_id,
                    now,
                ),
            )
        return PendingTextInput(
            owner_id=owner_id,
            draft_id=draft_id,
            base_text_version=base_text_version,
            mode=mode,
            prompt_message_id=prompt_message_id,
        )

    def get_pending(self, *, owner_id: int) -> PendingTextInput | None:
        row = self._connection.execute(
            """
            SELECT owner_id, draft_id, base_text_version, mode, prompt_message_id
            FROM pending_text_inputs WHERE owner_id = ?
            """,
            (owner_id,),
        ).fetchone()
        if row is None:
            return None
        return PendingTextInput(
            owner_id=int(row["owner_id"]),
            draft_id=str(row["draft_id"]),
            base_text_version=int(row["base_text_version"]),
            mode=TextInputMode(str(row["mode"])),
            prompt_message_id=int(row["prompt_message_id"]),
        )

    def consume(self, *, pending: PendingTextInput) -> None:
        """Delete only the exact binding that the caller applied."""

        with immediate_transaction(self._connection):
            cursor = self._connection.execute(
                """
                DELETE FROM pending_text_inputs
                WHERE owner_id = ? AND draft_id = ? AND base_text_version = ?
                  AND mode = ? AND prompt_message_id = ?
                """,
                (
                    pending.owner_id,
                    pending.draft_id,
                    pending.base_text_version,
                    pending.mode.value,
                    pending.prompt_message_id,
                ),
            )
            if cursor.rowcount != 1:
                raise EditingError("pending text input changed before consumption")

    def record_revision_request(
        self,
        *,
        job_id: int,
        pending: PendingTextInput,
        comment: str,
        source_kind: str,
        now: str,
    ) -> TextRevisionRequest:
        """Attach one private revision instruction to its exact paid job."""

        if job_id <= 0:
            raise ValueError("job_id must be positive")
        if not comment.strip() or not now.strip():
            raise ValueError("comment and now must not be blank")
        if source_kind not in {"text", "voice"}:
            raise ValueError("source_kind must be text or voice")
        with immediate_transaction(self._connection):
            self._connection.execute(
                """
                INSERT INTO text_revision_requests (
                    job_id, draft_id, base_text_version, comment, source_kind, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    pending.draft_id,
                    pending.base_text_version,
                    comment,
                    source_kind,
                    now,
                ),
            )
        return TextRevisionRequest(
            job_id=job_id,
            draft_id=pending.draft_id,
            base_text_version=pending.base_text_version,
            comment=comment,
            source_kind=source_kind,
        )

    def get_revision_request(self, *, job_id: int) -> TextRevisionRequest | None:
        row = self._connection.execute(
            """
            SELECT job_id, draft_id, base_text_version, comment, source_kind
            FROM text_revision_requests WHERE job_id = ?
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        return TextRevisionRequest(
            job_id=int(row["job_id"]),
            draft_id=str(row["draft_id"]),
            base_text_version=int(row["base_text_version"]),
            comment=str(row["comment"]),
            source_kind=str(row["source_kind"]),
        )


class ImageEditingRepository:
    """Persist exact image review context without exposing revision comments."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def bind(
        self,
        *,
        owner_id: int,
        draft_id: str,
        base_image_version: int,
        source_text_version: int,
        mode: ImageInputMode,
        prompt_message_id: int,
        now: str,
    ) -> PendingImageInput:
        """Bind the next response, rejecting ambiguous simultaneous bindings."""

        if (
            owner_id <= 0
            or base_image_version < 0
            or source_text_version <= 0
            or prompt_message_id < 0
        ):
            raise ValueError("image binding identifiers are invalid")
        if mode is ImageInputMode.AI_REVISION and base_image_version == 0:
            raise ValueError("AI image revision requires an existing image")
        if not draft_id.strip() or not now.strip():
            raise ValueError("draft_id and now must not be blank")
        with immediate_transaction(self._connection):
            if self.get_pending(owner_id=owner_id) is not None or _has_pending_text(
                self._connection, owner_id=owner_id
            ):
                raise PendingInputConflict("another owner input is already pending")
            self._connection.execute(
                """
                INSERT INTO pending_image_inputs (
                    owner_id, draft_id, base_image_version, source_text_version,
                    mode, prompt_message_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    owner_id,
                    draft_id,
                    base_image_version,
                    source_text_version,
                    mode.value,
                    prompt_message_id,
                    now,
                ),
            )
        return PendingImageInput(
            owner_id=owner_id,
            draft_id=draft_id,
            base_image_version=base_image_version,
            source_text_version=source_text_version,
            mode=mode,
            prompt_message_id=prompt_message_id,
        )

    def get_pending(self, *, owner_id: int) -> PendingImageInput | None:
        row = self._connection.execute(
            """
            SELECT owner_id, draft_id, base_image_version, source_text_version,
                   mode, prompt_message_id
            FROM pending_image_inputs WHERE owner_id = ?
            """,
            (owner_id,),
        ).fetchone()
        if row is None:
            return None
        return PendingImageInput(
            owner_id=int(row["owner_id"]),
            draft_id=str(row["draft_id"]),
            base_image_version=int(row["base_image_version"]),
            source_text_version=int(row["source_text_version"]),
            mode=ImageInputMode(str(row["mode"])),
            prompt_message_id=int(row["prompt_message_id"]),
        )

    def consume(self, *, pending: PendingImageInput) -> None:
        """Delete only the exact image binding that the caller applied."""

        with immediate_transaction(self._connection):
            cursor = self._connection.execute(
                """
                DELETE FROM pending_image_inputs
                WHERE owner_id = ? AND draft_id = ? AND base_image_version = ?
                  AND source_text_version = ? AND mode = ? AND prompt_message_id = ?
                """,
                (
                    pending.owner_id,
                    pending.draft_id,
                    pending.base_image_version,
                    pending.source_text_version,
                    pending.mode.value,
                    pending.prompt_message_id,
                ),
            )
            if cursor.rowcount != 1:
                raise EditingError("pending image input changed before consumption")

    def record_revision_request(
        self,
        *,
        job_id: int,
        pending: PendingImageInput,
        comment: str,
        now: str,
    ) -> ImageRevisionRequest:
        """Attach one private image revision instruction to its exact paid job."""

        if job_id <= 0:
            raise ValueError("job_id must be positive")
        if not comment.strip() or not now.strip():
            raise ValueError("comment and now must not be blank")
        with immediate_transaction(self._connection):
            self._connection.execute(
                """
                INSERT INTO image_revision_requests (
                    job_id, draft_id, base_image_version, source_text_version,
                    comment, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    pending.draft_id,
                    pending.base_image_version,
                    pending.source_text_version,
                    comment,
                    now,
                ),
            )
        return ImageRevisionRequest(
            job_id=job_id,
            draft_id=pending.draft_id,
            base_image_version=pending.base_image_version,
            source_text_version=pending.source_text_version,
            comment=comment,
        )

    def get_revision_request(self, *, job_id: int) -> ImageRevisionRequest | None:
        row = self._connection.execute(
            """
            SELECT job_id, draft_id, base_image_version, source_text_version, comment
            FROM image_revision_requests WHERE job_id = ?
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        return ImageRevisionRequest(
            job_id=int(row["job_id"]),
            draft_id=str(row["draft_id"]),
            base_image_version=int(row["base_image_version"]),
            source_text_version=int(row["source_text_version"]),
            comment=str(row["comment"]),
        )


class VoiceEditingRepository:
    """Persist a voice instruction and its exact dependent revision job."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def record(
        self,
        *,
        transcription_job_id: int,
        generation_job_id: int,
        owner_id: int,
        draft_id: str,
        target: VoiceRevisionTarget,
        base_text_version: int,
        base_image_version: int | None,
        prompt_message_id: int,
        telegram_update_id: int,
        file_unique_id: str,
        duration_seconds: int,
        file_size: int | None,
        mime_type: str,
        now: str,
    ) -> VoiceRevisionInput:
        """Record one exact voice binding after both paid jobs are reserved."""

        if (
            transcription_job_id <= 0
            or generation_job_id <= 0
            or owner_id <= 0
            or base_text_version <= 0
            or prompt_message_id < 0
            or telegram_update_id < 0
            or duration_seconds < 0
            or (file_size is not None and file_size < 0)
        ):
            raise ValueError("voice revision identifiers are invalid")
        if target is VoiceRevisionTarget.TEXT and base_image_version is not None:
            raise ValueError("text voice revision must not have an image version")
        if target is VoiceRevisionTarget.IMAGE and (
            base_image_version is None or base_image_version <= 0
        ):
            raise ValueError("image voice revision requires an image version")
        if not all(
            value.strip() for value in (draft_id, file_unique_id, mime_type, now)
        ):
            raise ValueError("voice revision text fields must not be blank")
        with immediate_transaction(self._connection):
            self._connection.execute(
                """
                INSERT INTO voice_revision_inputs (
                    transcription_job_id, generation_job_id, owner_id, draft_id,
                    target_kind, base_text_version, base_image_version,
                    prompt_message_id, telegram_update_id, file_unique_id,
                    duration_seconds, file_size, mime_type, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    transcription_job_id,
                    generation_job_id,
                    owner_id,
                    draft_id,
                    target.value,
                    base_text_version,
                    base_image_version,
                    prompt_message_id,
                    telegram_update_id,
                    file_unique_id,
                    duration_seconds,
                    file_size,
                    mime_type,
                    now,
                ),
            )
        return VoiceRevisionInput(
            transcription_job_id=transcription_job_id,
            generation_job_id=generation_job_id,
            owner_id=owner_id,
            draft_id=draft_id,
            target=target,
            base_text_version=base_text_version,
            base_image_version=base_image_version,
            prompt_message_id=prompt_message_id,
            telegram_update_id=telegram_update_id,
            file_unique_id=file_unique_id,
            duration_seconds=duration_seconds,
            file_size=file_size,
            mime_type=mime_type,
        )

    def get(self, *, transcription_job_id: int) -> VoiceRevisionInput | None:
        row = self._connection.execute(
            """
            SELECT transcription_job_id, generation_job_id, owner_id, draft_id,
                   target_kind, base_text_version, base_image_version,
                   prompt_message_id, telegram_update_id, file_unique_id,
                   duration_seconds, file_size, mime_type
            FROM voice_revision_inputs
            WHERE transcription_job_id = ?
            """,
            (transcription_job_id,),
        ).fetchone()
        return _voice_revision_input(row)

    def get_active(self, *, owner_id: int) -> VoiceRevisionInput | None:
        row = self._connection.execute(
            """
            SELECT voice_revision_inputs.transcription_job_id,
                   voice_revision_inputs.generation_job_id,
                   voice_revision_inputs.owner_id,
                   voice_revision_inputs.draft_id,
                   voice_revision_inputs.target_kind,
                   voice_revision_inputs.base_text_version,
                   voice_revision_inputs.base_image_version,
                   voice_revision_inputs.prompt_message_id,
                   voice_revision_inputs.telegram_update_id,
                   voice_revision_inputs.file_unique_id,
                   voice_revision_inputs.duration_seconds,
                   voice_revision_inputs.file_size,
                   voice_revision_inputs.mime_type
            FROM voice_revision_inputs
            JOIN jobs ON jobs.id = voice_revision_inputs.transcription_job_id
            WHERE voice_revision_inputs.owner_id = ?
              AND jobs.state IN ('QUEUED', 'LEASED', 'RESULT_UNKNOWN')
            ORDER BY voice_revision_inputs.transcription_job_id DESC
            LIMIT 1
            """,
            (owner_id,),
        ).fetchone()
        return _voice_revision_input(row)


def _has_pending_text(connection: sqlite3.Connection, *, owner_id: int) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM pending_text_inputs WHERE owner_id = ?",
            (owner_id,),
        ).fetchone()
        is not None
    )


def _has_pending_image(connection: sqlite3.Connection, *, owner_id: int) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM pending_image_inputs WHERE owner_id = ?",
            (owner_id,),
        ).fetchone()
        is not None
    )


def _voice_revision_input(row: sqlite3.Row | None) -> VoiceRevisionInput | None:
    if row is None:
        return None
    return VoiceRevisionInput(
        transcription_job_id=int(row["transcription_job_id"]),
        generation_job_id=int(row["generation_job_id"]),
        owner_id=int(row["owner_id"]),
        draft_id=str(row["draft_id"]),
        target=VoiceRevisionTarget(str(row["target_kind"])),
        base_text_version=int(row["base_text_version"]),
        base_image_version=(
            int(row["base_image_version"])
            if row["base_image_version"] is not None
            else None
        ),
        prompt_message_id=int(row["prompt_message_id"]),
        telegram_update_id=int(row["telegram_update_id"]),
        file_unique_id=str(row["file_unique_id"]),
        duration_seconds=int(row["duration_seconds"]),
        file_size=(int(row["file_size"]) if row["file_size"] is not None else None),
        mime_type=str(row["mime_type"]),
    )
