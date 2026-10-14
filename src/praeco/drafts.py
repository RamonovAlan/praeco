"""Transactional persistence for the version-bound draft workflow."""

import sqlite3
from dataclasses import dataclass, field

from praeco.domain import DraftState
from praeco.storage import immediate_transaction
from praeco.workflow import DraftWorkflow, PublicationSnapshotRef


class DraftNotFoundError(LookupError):
    """Raised when a requested draft does not exist."""


class DraftPersistenceConflict(RuntimeError):
    """Raised when persisted state changed before a guarded update."""


@dataclass(frozen=True, slots=True)
class VoiceSource:
    """Telegram voice metadata needed for one later download and transcription."""

    file_id: str = field(repr=False)
    file_unique_id: str
    duration_seconds: int
    file_size: int | None = None
    mime_type: str | None = None

    def __post_init__(self) -> None:
        if not self.file_id.strip() or not self.file_unique_id.strip():
            raise ValueError("voice file identifiers must not be blank")
        if self.duration_seconds < 0:
            raise ValueError("voice duration must not be negative")
        if self.file_size is not None and self.file_size < 0:
            raise ValueError("voice file size must not be negative")
        if self.mime_type is not None and not self.mime_type.strip():
            raise ValueError("voice mime type must not be blank")


@dataclass(frozen=True, slots=True)
class DraftIdentity:
    """Immutable source fields required when creating a draft."""

    draft_id: str
    owner_id: int
    source_kind: str
    source_text: str = field(repr=False)
    language: str
    voice_source: VoiceSource | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        DraftWorkflow(draft_id=self.draft_id, owner_id=self.owner_id)
        if self.source_kind not in {"text", "voice"}:
            raise ValueError("source_kind must be text or voice")
        if not self.language.strip():
            raise ValueError("language must not be blank")
        if self.source_kind == "text":
            if not self.source_text.strip():
                raise ValueError("text source must not be blank")
            if self.voice_source is not None:
                raise ValueError("text source must not include voice metadata")
        elif self.voice_source is None:
            raise ValueError("voice source metadata is required")


@dataclass(frozen=True, slots=True)
class DraftSourceRecord:
    """Private source data loaded only by the worker for one draft."""

    source_kind: str
    source_text: str = field(repr=False)
    language: str
    voice_source: VoiceSource | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class TextVersionRecord:
    """One immutable stored text version needed by a later provider stage."""

    version: int
    body: str = field(repr=False)
    revision_comment: str | None = field(default=None, repr=False)
    image_brief: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ImageVersionRecord:
    """One immutable stored image version needed by a later revision stage."""

    version: int
    source_text_version: int
    brief: str = field(repr=False)
    prompt: str | None = field(default=None, repr=False)
    local_path: str = field(default="", repr=False)
    sha256: str = field(default="", repr=False)
    mime_type: str = ""
    width: int = 0
    height: int = 0


class DraftRepository:
    """Persist workflow transitions while retaining immutable version history."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def create(self, *, identity: DraftIdentity, now: str) -> DraftWorkflow:
        if not now.strip():
            raise ValueError("now must not be blank")
        workflow = DraftWorkflow(draft_id=identity.draft_id, owner_id=identity.owner_id)
        with immediate_transaction(self._connection):
            self._connection.execute(
                """
                INSERT INTO drafts (
                    id, owner_id, source_kind, source_text, language, state,
                    source_file_id, source_file_unique_id, source_mime_type,
                    source_file_size, source_duration_seconds,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identity.draft_id,
                    identity.owner_id,
                    identity.source_kind,
                    identity.source_text,
                    identity.language,
                    workflow.state.value,
                    identity.voice_source.file_id if identity.voice_source else None,
                    (
                        identity.voice_source.file_unique_id
                        if identity.voice_source
                        else None
                    ),
                    identity.voice_source.mime_type if identity.voice_source else None,
                    identity.voice_source.file_size if identity.voice_source else None,
                    (
                        identity.voice_source.duration_seconds
                        if identity.voice_source
                        else None
                    ),
                    now,
                    now,
                ),
            )
        return workflow

    def record_transcript(
        self,
        *,
        draft_id: str,
        actor_id: int,
        transcript: str,
        language: str,
        now: str,
    ) -> DraftWorkflow:
        """Persist one voice transcript before text generation starts."""

        if not transcript.strip() or not language.strip():
            raise ValueError("transcript and language must not be blank")
        with immediate_transaction(self._connection):
            workflow = self._load(draft_id)
            if workflow.owner_id != actor_id:
                raise DraftPersistenceConflict("actor is not the draft owner")
            if workflow.state is not DraftState.RECEIVED:
                raise DraftPersistenceConflict("transcript requires a received draft")
            cursor = self._connection.execute(
                """
                UPDATE drafts
                SET source_text = ?, language = ?, updated_at = ?
                WHERE id = ? AND owner_id = ? AND source_kind = 'voice'
                  AND state = 'RECEIVED' AND length(trim(source_text)) = 0
                """,
                (transcript, language, now, draft_id, actor_id),
            )
            if cursor.rowcount != 1:
                raise DraftPersistenceConflict("voice transcript was already recorded")
            return workflow

    def get(self, draft_id: str) -> DraftWorkflow:
        """Load current pointers without exposing source text to unrelated callers."""

        return self._load(draft_id)

    def get_source(self, draft_id: str) -> DraftSourceRecord:
        """Load private source content for a leased worker job."""

        row = self._connection.execute(
            """
            SELECT source_kind, source_text, language, source_file_id,
                   source_file_unique_id, source_mime_type, source_file_size,
                   source_duration_seconds
            FROM drafts WHERE id = ?
            """,
            (draft_id,),
        ).fetchone()
        if row is None:
            raise DraftNotFoundError(f"draft not found: {draft_id}")
        voice_source = None
        if str(row["source_kind"]) == "voice":
            voice_source = VoiceSource(
                file_id=str(row["source_file_id"]),
                file_unique_id=str(row["source_file_unique_id"]),
                duration_seconds=int(row["source_duration_seconds"]),
                file_size=_optional_int(row["source_file_size"]),
                mime_type=_optional_text(row["source_mime_type"]),
            )
        return DraftSourceRecord(
            source_kind=str(row["source_kind"]),
            source_text=str(row["source_text"]),
            language=str(row["language"]),
            voice_source=voice_source,
        )

    def get_text_version(self, *, draft_id: str, version: int) -> TextVersionRecord:
        """Load one exact immutable text version without selecting a newer revision."""

        row = self._connection.execute(
            """
            SELECT version, body, revision_comment, image_brief
            FROM text_versions WHERE draft_id = ? AND version = ?
            """,
            (draft_id, version),
        ).fetchone()
        if row is None:
            raise DraftNotFoundError(f"text version not found: {draft_id}")
        return TextVersionRecord(
            version=int(row["version"]),
            body=str(row["body"]),
            revision_comment=_optional_text(row["revision_comment"]),
            image_brief=_optional_text(row["image_brief"]),
        )

    def get_image_version(self, *, draft_id: str, version: int) -> ImageVersionRecord:
        """Load one exact immutable image version for a bound revision."""

        row = self._connection.execute(
            """
            SELECT version, source_text_version, brief, prompt, local_path, sha256,
                   mime_type, width, height
            FROM image_versions WHERE draft_id = ? AND version = ?
            """,
            (draft_id, version),
        ).fetchone()
        if row is None:
            raise DraftNotFoundError(f"image version not found: {draft_id}")
        return ImageVersionRecord(
            version=int(row["version"]),
            source_text_version=int(row["source_text_version"]),
            brief=str(row["brief"]),
            prompt=_optional_text(row["prompt"]),
            local_path=str(row["local_path"]),
            sha256=str(row["sha256"]),
            mime_type=str(row["mime_type"]),
            width=int(row["width"]),
            height=int(row["height"]),
        )

    def start_text_generation(
        self,
        *,
        draft_id: str,
        actor_id: int,
        now: str,
    ) -> DraftWorkflow:
        with immediate_transaction(self._connection):
            previous = self._load(draft_id)
            workflow = _copy_workflow(previous)
            workflow.start_text_generation(actor_id=actor_id)
            self._save_guarded(previous=previous, workflow=workflow, now=now)
            return workflow

    def record_text(
        self,
        *,
        draft_id: str,
        actor_id: int,
        version: int,
        body: str,
        revision_comment: str | None,
        prompt_version: str | None,
        style_version: str | None,
        provider_model: str | None,
        now: str,
        image_brief: str | None = None,
    ) -> DraftWorkflow:
        if not body.strip():
            raise ValueError("body must not be blank")
        with immediate_transaction(self._connection):
            previous = self._load(draft_id)
            workflow = _copy_workflow(previous)
            workflow.record_text(actor_id=actor_id, version=version)
            self._connection.execute(
                """
                INSERT INTO text_versions (
                    draft_id, version, body, revision_comment, prompt_version,
                    style_version, provider_model, image_brief, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft_id,
                    version,
                    body,
                    revision_comment,
                    prompt_version,
                    style_version,
                    provider_model,
                    image_brief,
                    now,
                ),
            )
            self._save_guarded(previous=previous, workflow=workflow, now=now)
            return workflow

    def approve_text(
        self,
        *,
        draft_id: str,
        actor_id: int,
        version: int,
        now: str,
    ) -> DraftWorkflow:
        with immediate_transaction(self._connection):
            previous = self._load(draft_id)
            workflow = _copy_workflow(previous)
            workflow.approve_text(actor_id=actor_id, version=version)
            cursor = self._connection.execute(
                """
                UPDATE text_versions SET approved_at = ?
                WHERE draft_id = ? AND version = ?
                """,
                (now, draft_id, version),
            )
            if cursor.rowcount != 1:
                raise DraftPersistenceConflict("text version disappeared during approval")
            self._save_guarded(previous=previous, workflow=workflow, now=now)
            return workflow

    def mark_generation_failed(self, *, draft_id: str, now: str) -> DraftWorkflow:
        """Persist a recoverable provider failure without changing version pointers."""

        with immediate_transaction(self._connection):
            previous = self._load(draft_id)
            workflow = _copy_workflow(previous)
            workflow.mark_generation_failed()
            self._save_guarded(previous=previous, workflow=workflow, now=now)
            return workflow

    def wait_for_text_input(
        self,
        *,
        draft_id: str,
        actor_id: int,
        version: int,
        now: str,
    ) -> DraftWorkflow:
        """Persist an explicit request for revision or manual text input."""

        with immediate_transaction(self._connection):
            previous = self._load(draft_id)
            workflow = _copy_workflow(previous)
            workflow.wait_for_text_input(actor_id=actor_id, version=version)
            self._save_guarded(previous=previous, workflow=workflow, now=now)
            return workflow

    def stop_waiting_for_text_input(
        self,
        *,
        draft_id: str,
        actor_id: int,
        version: int,
        now: str,
    ) -> DraftWorkflow:
        """Abandon one pending edit without changing its immutable text."""

        with immediate_transaction(self._connection):
            previous = self._load(draft_id)
            workflow = _copy_workflow(previous)
            workflow.stop_waiting_for_text_input(actor_id=actor_id, version=version)
            self._save_guarded(previous=previous, workflow=workflow, now=now)
            return workflow

    def wait_for_image_input(
        self,
        *,
        draft_id: str,
        actor_id: int,
        version: int,
        now: str,
    ) -> DraftWorkflow:
        """Persist an explicit request for image revision or owner upload."""

        with immediate_transaction(self._connection):
            previous = self._load(draft_id)
            workflow = _copy_workflow(previous)
            workflow.wait_for_image_input(actor_id=actor_id, version=version)
            self._save_guarded(previous=previous, workflow=workflow, now=now)
            return workflow

    def stop_waiting_for_image_input(
        self,
        *,
        draft_id: str,
        actor_id: int,
        version: int,
        now: str,
    ) -> DraftWorkflow:
        """Abandon one image binding without changing its immutable image."""

        with immediate_transaction(self._connection):
            previous = self._load(draft_id)
            workflow = _copy_workflow(previous)
            workflow.stop_waiting_for_image_input(actor_id=actor_id, version=version)
            self._save_guarded(previous=previous, workflow=workflow, now=now)
            return workflow

    def return_to_text_review(
        self,
        *,
        draft_id: str,
        actor_id: int,
        image_version: int,
        now: str,
    ) -> DraftWorkflow:
        """Return from an exact image preview to the current text review."""

        with immediate_transaction(self._connection):
            previous = self._load(draft_id)
            workflow = _copy_workflow(previous)
            workflow.return_to_text_review(
                actor_id=actor_id,
                image_version=image_version,
            )
            self._save_guarded(previous=previous, workflow=workflow, now=now)
            return workflow

    def start_image_generation(
        self,
        *,
        draft_id: str,
        actor_id: int,
        now: str,
    ) -> DraftWorkflow:
        with immediate_transaction(self._connection):
            previous = self._load(draft_id)
            workflow = _copy_workflow(previous)
            workflow.start_image_generation(actor_id=actor_id)
            self._save_guarded(previous=previous, workflow=workflow, now=now)
            return workflow

    def record_image(
        self,
        *,
        draft_id: str,
        actor_id: int,
        version: int,
        source_text_version: int,
        brief: str,
        prompt: str | None,
        local_path: str,
        sha256: str,
        mime_type: str,
        width: int,
        height: int,
        now: str,
    ) -> DraftWorkflow:
        with immediate_transaction(self._connection):
            previous = self._load(draft_id)
            workflow = _copy_workflow(previous)
            workflow.record_image(
                actor_id=actor_id,
                version=version,
                source_text_version=source_text_version,
            )
            self._connection.execute(
                """
                INSERT INTO image_versions (
                    draft_id, version, source_text_version, brief, prompt, local_path,
                    sha256, mime_type, width, height, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft_id,
                    version,
                    source_text_version,
                    brief,
                    prompt,
                    local_path,
                    sha256,
                    mime_type,
                    width,
                    height,
                    now,
                ),
            )
            self._save_guarded(previous=previous, workflow=workflow, now=now)
            return workflow

    def record_owner_image(
        self,
        *,
        draft_id: str,
        actor_id: int,
        version: int,
        source_text_version: int,
        brief: str,
        local_path: str,
        sha256: str,
        mime_type: str,
        width: int,
        height: int,
        now: str,
    ) -> DraftWorkflow:
        """Persist one owner-provided original without creating provider usage."""

        with immediate_transaction(self._connection):
            previous = self._load(draft_id)
            workflow = _copy_workflow(previous)
            workflow.record_owner_image(
                actor_id=actor_id,
                version=version,
                source_text_version=source_text_version,
            )
            self._connection.execute(
                """
                INSERT INTO image_versions (
                    draft_id, version, source_text_version, brief, prompt, local_path,
                    sha256, mime_type, width, height, created_at
                ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft_id,
                    version,
                    source_text_version,
                    brief,
                    local_path,
                    sha256,
                    mime_type,
                    width,
                    height,
                    now,
                ),
            )
            self._save_guarded(previous=previous, workflow=workflow, now=now)
            return workflow

    def approve_image(
        self,
        *,
        draft_id: str,
        actor_id: int,
        version: int,
        now: str,
    ) -> DraftWorkflow:
        with immediate_transaction(self._connection):
            previous = self._load(draft_id)
            workflow = _copy_workflow(previous)
            workflow.approve_image(actor_id=actor_id, version=version)
            cursor = self._connection.execute(
                """
                UPDATE image_versions SET approved_at = ?
                WHERE draft_id = ? AND version = ?
                """,
                (now, draft_id, version),
            )
            if cursor.rowcount != 1:
                raise DraftPersistenceConflict("image version disappeared during approval")
            self._save_guarded(previous=previous, workflow=workflow, now=now)
            return workflow

    def cancel(
        self,
        *,
        draft_id: str,
        actor_id: int,
        expected_text_version: int | None,
        expected_image_version: int | None,
        now: str,
    ) -> DraftWorkflow:
        """Cancel only the draft version for which the owner saw a button."""

        if (expected_text_version is None) == (expected_image_version is None):
            raise ValueError("cancel must bind to exactly one version kind")
        with immediate_transaction(self._connection):
            previous = self._load(draft_id)
            if (
                expected_text_version is not None
                and previous.current_text_version != expected_text_version
            ):
                raise DraftPersistenceConflict("cancel callback refers to a stale version")
            if (
                expected_image_version is not None
                and previous.current_image_version != expected_image_version
            ):
                raise DraftPersistenceConflict("cancel callback refers to a stale version")
            workflow = _copy_workflow(previous)
            workflow.cancel(actor_id=actor_id)
            self._save_guarded(previous=previous, workflow=workflow, now=now)
            return workflow

    def publication_snapshot(
        self,
        *,
        draft_id: str,
        actor_id: int,
    ) -> PublicationSnapshotRef:
        return self._load(draft_id).publication_snapshot(actor_id=actor_id)

    def begin_publish(
        self,
        *,
        draft_id: str,
        actor_id: int,
        snapshot: PublicationSnapshotRef,
        now: str,
    ) -> DraftWorkflow:
        with immediate_transaction(self._connection):
            previous = self._load(draft_id)
            workflow = _copy_workflow(previous)
            workflow.begin_publish(actor_id=actor_id, snapshot=snapshot)
            self._save_guarded(previous=previous, workflow=workflow, now=now)
            return workflow

    def mark_published(
        self,
        *,
        draft_id: str,
        external_id: str,
        now: str,
    ) -> DraftWorkflow:
        with immediate_transaction(self._connection):
            previous = self._load(draft_id)
            workflow = _copy_workflow(previous)
            workflow.mark_published(external_id=external_id)
            self._save_guarded(previous=previous, workflow=workflow, now=now)
            return workflow

    def mark_publish_failed(self, *, draft_id: str, now: str) -> DraftWorkflow:
        with immediate_transaction(self._connection):
            previous = self._load(draft_id)
            workflow = _copy_workflow(previous)
            workflow.mark_publish_failed()
            self._save_guarded(previous=previous, workflow=workflow, now=now)
            return workflow

    def mark_publish_unknown(self, *, draft_id: str, now: str) -> DraftWorkflow:
        with immediate_transaction(self._connection):
            previous = self._load(draft_id)
            workflow = _copy_workflow(previous)
            workflow.mark_publish_unknown()
            self._save_guarded(previous=previous, workflow=workflow, now=now)
            return workflow

    def _load(self, draft_id: str) -> DraftWorkflow:
        row = self._connection.execute(
            """
            SELECT id, owner_id, state, current_text_version, approved_text_version,
                   current_image_version, image_source_text_version,
                   approved_image_version
            FROM drafts
            WHERE id = ?
            """,
            (draft_id,),
        ).fetchone()
        if row is None:
            raise DraftNotFoundError(f"draft not found: {draft_id}")
        return DraftWorkflow(
            draft_id=str(row["id"]),
            owner_id=int(row["owner_id"]),
            state=DraftState(str(row["state"])),
            current_text_version=int(row["current_text_version"]),
            approved_text_version=_optional_int(row["approved_text_version"]),
            current_image_version=int(row["current_image_version"]),
            image_source_text_version=_optional_int(row["image_source_text_version"]),
            approved_image_version=_optional_int(row["approved_image_version"]),
        )

    def _save_guarded(
        self,
        *,
        previous: DraftWorkflow,
        workflow: DraftWorkflow,
        now: str,
    ) -> None:
        cursor = self._connection.execute(
            """
            UPDATE drafts
            SET state = ?,
                current_text_version = ?,
                approved_text_version = ?,
                current_image_version = ?,
                image_source_text_version = ?,
                approved_image_version = ?,
                updated_at = ?
            WHERE id = ?
              AND state = ?
              AND current_text_version = ?
              AND approved_text_version IS ?
              AND current_image_version = ?
              AND image_source_text_version IS ?
              AND approved_image_version IS ?
            """,
            (
                workflow.state.value,
                workflow.current_text_version,
                workflow.approved_text_version,
                workflow.current_image_version,
                workflow.image_source_text_version,
                workflow.approved_image_version,
                now,
                previous.draft_id,
                previous.state.value,
                previous.current_text_version,
                previous.approved_text_version,
                previous.current_image_version,
                previous.image_source_text_version,
                previous.approved_image_version,
            ),
        )
        if cursor.rowcount != 1:
            raise DraftPersistenceConflict("draft state or version changed concurrently")


def _copy_workflow(workflow: DraftWorkflow) -> DraftWorkflow:
    return DraftWorkflow(
        draft_id=workflow.draft_id,
        owner_id=workflow.owner_id,
        state=workflow.state,
        current_text_version=workflow.current_text_version,
        approved_text_version=workflow.approved_text_version,
        current_image_version=workflow.current_image_version,
        image_source_text_version=workflow.image_source_text_version,
        approved_image_version=workflow.approved_image_version,
        external_publication_id=workflow.external_publication_id,
    )


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    raise TypeError("SQLite approval pointer must be an integer or null")


def _optional_text(value: object) -> str | None:
    return str(value) if value is not None else None
