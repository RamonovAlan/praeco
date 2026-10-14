"""Durable exact-snapshot LinkedIn publication orchestration."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from praeco.drafts import DraftRepository
from praeco.linkedin import LinkedInOAuthError, LinkedInOAuthService
from praeco.linkedin_publish import (
    LinkedInPublicationRequest,
    LinkedInPublisher,
    LinkedInPublishError,
)
from praeco.outbox import DeliveryKind, DeliveryRepository, DeliveryRequest
from praeco.storage import immediate_transaction
from praeco.workflow import PublicationSnapshotRef


class PublicationError(RuntimeError):
    """Sanitized durable-publication failure."""


class PublicationWorkerOutcome(StrEnum):
    """Bounded worker result safe for runtime diagnostics."""

    IDLE = "IDLE"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    PUBLISH_UNKNOWN = "PUBLISH_UNKNOWN"


@dataclass(frozen=True, slots=True)
class PublicationLease:
    """One exact snapshot claimed for a single external create attempt."""

    publication_id: int
    draft_id: str
    account_id: str
    text_version: int
    image_version: int


@dataclass(frozen=True, slots=True)
class PublicationWorkerResult:
    """Non-sensitive result returned after one bounded worker iteration."""

    outcome: PublicationWorkerOutcome
    publication_id: int | None = None
    draft_id: str | None = None


class PublicationRepository:
    """Persist final clicks and terminal LinkedIn outcomes without network calls."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._drafts = DraftRepository(connection)
        self._deliveries = DeliveryRepository(connection)

    def enqueue(
        self,
        *,
        draft_id: str,
        actor_id: int,
        text_version: int,
        image_version: int,
        now: str,
    ) -> int:
        """Consume one exact final click and persist a non-retryable work item."""

        snapshot = PublicationSnapshotRef(
            draft_id=draft_id,
            text_version=text_version,
            image_version=image_version,
        )
        with immediate_transaction(self._connection):
            current = self._drafts.publication_snapshot(
                draft_id=draft_id,
                actor_id=actor_id,
            )
            if current != snapshot:
                raise PublicationError("publication callback is stale")
            connection = self._connection.execute(
                """
                SELECT account_id, scopes_json, expires_at
                FROM account_connections
                WHERE platform = 'linkedin'
                ORDER BY id
                LIMIT 1
                """
            ).fetchone()
            if connection is None:
                raise PublicationError("LinkedIn is not connected")
            try:
                scopes = frozenset(json.loads(str(connection["scopes_json"])))
            except (TypeError, json.JSONDecodeError):
                raise PublicationError("LinkedIn connection is invalid") from None
            if (
                not scopes >= {"openid", "profile", "w_member_social"}
                or str(connection["expires_at"]) <= now
            ):
                raise PublicationError("LinkedIn connection is expired or incomplete")
            account_id = str(connection["account_id"])
            content = self._connection.execute(
                """
                SELECT t.body, i.sha256
                FROM text_versions AS t
                JOIN image_versions AS i
                  ON i.draft_id = t.draft_id
                 AND i.source_text_version = t.version
                WHERE t.draft_id = ? AND t.version = ? AND i.version = ?
                  AND t.approved_at IS NOT NULL AND i.approved_at IS NOT NULL
                """,
                (draft_id, text_version, image_version),
            ).fetchone()
            if content is None:
                raise PublicationError("approved publication snapshot is missing")
            snapshot_hash = _snapshot_hash(
                draft_id=draft_id,
                text_version=text_version,
                image_version=image_version,
                body=str(content["body"]),
                image_sha256=str(content["sha256"]),
            )
            existing = self._connection.execute(
                """
                SELECT id, state FROM publications
                WHERE snapshot_hash = ? AND platform = 'linkedin' AND account_id = ?
                """,
                (snapshot_hash, account_id),
            ).fetchone()
            if existing is None:
                cursor = self._connection.execute(
                    """
                    INSERT INTO publications (
                        draft_id, snapshot_hash, text_version, image_version,
                        platform, account_id, visibility, state, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'linkedin', ?, 'PUBLIC', 'PENDING', ?, ?)
                    """,
                    (
                        draft_id,
                        snapshot_hash,
                        text_version,
                        image_version,
                        account_id,
                        now,
                        now,
                    ),
                )
                if cursor.lastrowid is None:
                    raise PublicationError("publication insert returned no ID")
                publication_id = cursor.lastrowid
            else:
                if str(existing["state"]) != "FAILED":
                    raise PublicationError("publication snapshot was already submitted")
                publication_id = int(existing["id"])
                self._connection.execute(
                    """
                    UPDATE publications
                    SET state = 'PENDING', error_category = NULL, updated_at = ?
                    WHERE id = ? AND state = 'FAILED'
                    """,
                    (now, publication_id),
                )
            self._drafts.begin_publish(
                draft_id=draft_id,
                actor_id=actor_id,
                snapshot=snapshot,
                now=now,
            )
            return publication_id

    def claim_next(self, *, now: str) -> PublicationLease | None:
        """Claim one pending create exactly once; in-flight work is never reclaimed."""

        with immediate_transaction(self._connection):
            row = self._connection.execute(
                """
                SELECT id, draft_id, account_id, text_version, image_version
                FROM publications
                WHERE state = 'PENDING'
                ORDER BY id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            publication_id = int(row["id"])
            cursor = self._connection.execute(
                """
                UPDATE publications SET state = 'PUBLISHING', updated_at = ?
                WHERE id = ? AND state = 'PENDING'
                """,
                (now, publication_id),
            )
            if cursor.rowcount != 1:
                raise PublicationError("publication changed during claim")
            return PublicationLease(
                publication_id=publication_id,
                draft_id=str(row["draft_id"]),
                account_id=str(row["account_id"]),
                text_version=int(row["text_version"]),
                image_version=int(row["image_version"]),
            )

    def payload(
        self,
        *,
        lease: PublicationLease,
        media_directory: Path,
        access_token: str,
    ) -> LinkedInPublicationRequest:
        row = self._connection.execute(
            """
            SELECT t.body, i.local_path, i.sha256, i.mime_type
            FROM publications AS p
            JOIN text_versions AS t
              ON t.draft_id = p.draft_id AND t.version = p.text_version
            JOIN image_versions AS i
              ON i.draft_id = p.draft_id AND i.version = p.image_version
            WHERE p.id = ? AND p.state = 'PUBLISHING'
              AND p.draft_id = ? AND p.text_version = ? AND p.image_version = ?
            """,
            (
                lease.publication_id,
                lease.draft_id,
                lease.text_version,
                lease.image_version,
            ),
        ).fetchone()
        if row is None:
            raise PublicationError("publication payload is missing")
        image_path = _validated_image_path(
            media_directory=media_directory,
            local_path=str(row["local_path"]),
            expected_sha256=str(row["sha256"]),
        )
        return LinkedInPublicationRequest(
            account_id=lease.account_id,
            access_token=access_token,
            commentary=str(row["body"]),
            image_content=image_path.read_bytes(),
            image_mime_type=str(row["mime_type"]),
        )

    def mark_published(
        self,
        *,
        lease: PublicationLease,
        external_id: str,
        now: str,
    ) -> None:
        with immediate_transaction(self._connection):
            self._finish(
                lease=lease,
                state="PUBLISHED",
                external_id=external_id,
                error_category=None,
                now=now,
            )
            self._drafts.mark_published(
                draft_id=lease.draft_id,
                external_id=external_id,
                now=now,
            )
            self._enqueue_result(lease=lease, now=now)

    def mark_failed(
        self,
        *,
        lease: PublicationLease,
        error_category: str,
        now: str,
    ) -> None:
        with immediate_transaction(self._connection):
            self._finish(
                lease=lease,
                state="FAILED",
                external_id=None,
                error_category=error_category,
                now=now,
            )
            self._drafts.mark_publish_failed(draft_id=lease.draft_id, now=now)
            self._enqueue_result(lease=lease, now=now)

    def mark_unknown(
        self,
        *,
        lease: PublicationLease,
        now: str,
    ) -> None:
        with immediate_transaction(self._connection):
            self._finish(
                lease=lease,
                state="PUBLISH_UNKNOWN",
                external_id=None,
                error_category="provider_result_unknown",
                now=now,
            )
            self._drafts.mark_publish_unknown(draft_id=lease.draft_id, now=now)
            self._enqueue_result(lease=lease, now=now)

    def recover_inflight_as_unknown(self, *, now: str) -> int:
        """Stop after restarts because an in-flight POST may have succeeded."""

        rows = self._connection.execute(
            """
            SELECT id, draft_id, account_id, text_version, image_version
            FROM publications WHERE state = 'PUBLISHING'
            """
        ).fetchall()
        recovered = 0
        for row in rows:
            with immediate_transaction(self._connection):
                cursor = self._connection.execute(
                    """
                    UPDATE publications
                    SET state = 'PUBLISH_UNKNOWN',
                        error_category = 'process_interrupted', updated_at = ?
                    WHERE id = ? AND state = 'PUBLISHING'
                    """,
                    (now, int(row["id"])),
                )
                if cursor.rowcount != 1:
                    continue
                self._drafts.mark_publish_unknown(
                    draft_id=str(row["draft_id"]),
                    now=now,
                )
                lease = PublicationLease(
                    publication_id=int(row["id"]),
                    draft_id=str(row["draft_id"]),
                    account_id=str(row["account_id"]),
                    text_version=int(row["text_version"]),
                    image_version=int(row["image_version"]),
                )
                self._enqueue_result(lease=lease, now=now)
                recovered += 1
        return recovered

    def _finish(
        self,
        *,
        lease: PublicationLease,
        state: str,
        external_id: str | None,
        error_category: str | None,
        now: str,
    ) -> None:
        cursor = self._connection.execute(
            """
            UPDATE publications
            SET state = ?, external_id = ?, api_confirmed_at = ?,
                error_category = ?, updated_at = ?
            WHERE id = ? AND state = 'PUBLISHING'
            """,
            (
                state,
                external_id,
                now if state == "PUBLISHED" else None,
                error_category,
                now,
                lease.publication_id,
            ),
        )
        if cursor.rowcount != 1:
            raise PublicationError("publication is not in progress")

    def _enqueue_result(self, *, lease: PublicationLease, now: str) -> None:
        owner = self._connection.execute(
            "SELECT owner_id FROM drafts WHERE id = ?",
            (lease.draft_id,),
        ).fetchone()
        if owner is None:
            raise PublicationError("publication owner is missing")
        state_row = self._connection.execute(
            "SELECT state FROM publications WHERE id = ?",
            (lease.publication_id,),
        ).fetchone()
        if state_row is None:
            raise PublicationError("publication result is missing")
        state = str(state_row["state"])
        self._deliveries.enqueue(
            DeliveryRequest(
                kind=DeliveryKind.PUBLICATION_RESULT,
                draft_id=lease.draft_id,
                chat_id=int(owner["owner_id"]),
                publication_id=lease.publication_id,
                deduplication_key=(
                    f"telegram:publication-result:{lease.publication_id}:{state}"
                ),
                now=now,
            )
        )


class PublicationWorker:
    """Execute at most one explicit LinkedIn create without automatic retry."""

    def __init__(
        self,
        *,
        connection: sqlite3.Connection,
        media_directory: Path,
        oauth: LinkedInOAuthService,
        publisher: LinkedInPublisher,
    ) -> None:
        self._repository = PublicationRepository(connection)
        self._media_directory = media_directory
        self._oauth = oauth
        self._publisher = publisher

    def run_once(self, *, now: str) -> PublicationWorkerResult:
        lease = self._repository.claim_next(now=now)
        if lease is None:
            return PublicationWorkerResult(outcome=PublicationWorkerOutcome.IDLE)
        try:
            credentials = self._oauth.publication_credentials(
                now=datetime.fromisoformat(now.replace("Z", "+00:00"))
            )
            if credentials.account_id != lease.account_id:
                raise PublicationError("connected LinkedIn account changed")
            request = self._repository.payload(
                lease=lease,
                media_directory=self._media_directory,
                access_token=credentials.access_token,
            )
        except (LinkedInOAuthError, PublicationError, OSError, ValueError):
            self._repository.mark_failed(
                lease=lease,
                error_category="publication_preflight_failed",
                now=now,
            )
            return PublicationWorkerResult(
                outcome=PublicationWorkerOutcome.FAILED,
                publication_id=lease.publication_id,
                draft_id=lease.draft_id,
            )
        try:
            result = self._publisher.publish(request)
        except LinkedInPublishError as error:
            if error.result_unknown:
                self._repository.mark_unknown(lease=lease, now=now)
                outcome = PublicationWorkerOutcome.PUBLISH_UNKNOWN
            else:
                self._repository.mark_failed(
                    lease=lease,
                    error_category="linkedin_rejected_publication",
                    now=now,
                )
                outcome = PublicationWorkerOutcome.FAILED
            return PublicationWorkerResult(
                outcome=outcome,
                publication_id=lease.publication_id,
                draft_id=lease.draft_id,
            )
        self._repository.mark_published(
            lease=lease,
            external_id=result.external_id,
            now=now,
        )
        return PublicationWorkerResult(
            outcome=PublicationWorkerOutcome.PUBLISHED,
            publication_id=lease.publication_id,
            draft_id=lease.draft_id,
        )


def _snapshot_hash(
    *,
    draft_id: str,
    text_version: int,
    image_version: int,
    body: str,
    image_sha256: str,
) -> str:
    canonical = json.dumps(
        {
            "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "draft_id": draft_id,
            "image_sha256": image_sha256,
            "image_version": image_version,
            "text_version": text_version,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _validated_image_path(
    *,
    media_directory: Path,
    local_path: str,
    expected_sha256: str,
) -> Path:
    media_root = media_directory.resolve()
    image_path = (media_root / local_path).resolve()
    if not image_path.is_relative_to(media_root) or not image_path.is_file():
        raise PublicationError("publication image is unavailable")
    with image_path.open("rb") as image_file:
        digest = hashlib.file_digest(image_file, "sha256").hexdigest()
    if digest != expected_sha256:
        raise PublicationError("publication image checksum does not match")
    return image_path
