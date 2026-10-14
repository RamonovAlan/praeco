"""Durable SQLite job queue with atomic budget reservations and safe leases."""

import re
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import cast

from praeco.budget import BudgetPolicy, ReservationDecision, ReservationStatus, SpendSnapshot
from praeco.storage import immediate_transaction

_BILLING_MONTH_PATTERN = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
_MICRODOLLARS_PER_DOLLAR = Decimal("1000000")


class QueueError(RuntimeError):
    """Raised when a requested queue transition is unsafe or inconsistent."""


class QueueOutcome(StrEnum):
    """Result of an idempotent paid-job enqueue attempt."""

    CREATED = "CREATED"
    CREATED_WITH_WARNING = "CREATED_WITH_WARNING"
    DUPLICATE = "DUPLICATE"
    BUDGET_BLOCKED = "BUDGET_BLOCKED"


@dataclass(frozen=True, slots=True)
class PaidJobRequest:
    """Immutable paid work request with a conservative cost reservation."""

    kind: str
    draft_id: str
    input_version: int
    deduplication_key: str
    estimated_cost_usd: Decimal
    billing_month: str
    now: str

    def __post_init__(self) -> None:
        for field_name in ("kind", "draft_id", "deduplication_key", "now"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} must not be blank")
        if self.input_version < 0:
            raise ValueError("input_version must not be negative")
        if self.estimated_cost_usd < 0:
            raise ValueError("estimated_cost_usd must not be negative")
        if _BILLING_MONTH_PATTERN.fullmatch(self.billing_month) is None:
            raise ValueError("billing_month must use YYYY-MM")


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    """Persistent identity and budget result for an enqueue attempt."""

    outcome: QueueOutcome
    job_id: int | None
    decision: ReservationDecision | None


@dataclass(frozen=True, slots=True)
class JobLease:
    """One job owned by a worker until its lease expires or execution begins."""

    job_id: int
    kind: str
    draft_id: str | None
    input_version: int
    attempts: int
    estimated_cost_usd: Decimal


@dataclass(frozen=True, slots=True)
class UsageMetadata:
    """Provider usage fields that are safe to persist after a paid call."""

    stage: str
    provider_model: str
    price_version: str
    now: str
    input_units: int | None = None
    output_units: int | None = None
    duration_seconds: Decimal | None = None
    image_quality: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("stage", "provider_model", "price_version", "now"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} must not be blank")
        for field_name in ("input_units", "output_units"):
            value = getattr(self, field_name)
            if value is not None and value < 0:
                raise ValueError(f"{field_name} must not be negative")
        if self.duration_seconds is not None and self.duration_seconds < 0:
            raise ValueError("duration_seconds must not be negative")
        if self.image_quality is not None and not self.image_quality.strip():
            raise ValueError("image_quality must not be blank")


class JobRepository:
    """Serialize queue and budget decisions through short SQLite transactions."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def enqueue_paid(
        self,
        *,
        request: PaidJobRequest,
        policy: BudgetPolicy,
    ) -> EnqueueResult:
        """Idempotently enqueue paid work after reserving budget atomically."""

        estimated_microusd = _usd_to_microusd(request.estimated_cost_usd)
        with immediate_transaction(self._connection):
            existing = self._connection.execute(
                "SELECT id FROM jobs WHERE deduplication_key = ?",
                (request.deduplication_key,),
            ).fetchone()
            if existing is not None:
                return EnqueueResult(
                    outcome=QueueOutcome.DUPLICATE,
                    job_id=int(existing["id"]),
                    decision=None,
                )

            decision = policy.assess_reservation(
                snapshot=self._spend_snapshot(
                    draft_id=request.draft_id,
                    billing_month=request.billing_month,
                ),
                estimated_call_usd=request.estimated_cost_usd,
            )
            if not decision.allowed:
                return EnqueueResult(
                    outcome=QueueOutcome.BUDGET_BLOCKED,
                    job_id=None,
                    decision=decision,
                )

            cursor = self._connection.execute(
                """
                INSERT INTO jobs (
                    kind,
                    draft_id,
                    input_version,
                    state,
                    next_attempt_at,
                    deduplication_key,
                    estimated_cost_microusd,
                    billing_month,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, 'QUEUED', ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.kind,
                    request.draft_id,
                    request.input_version,
                    request.now,
                    request.deduplication_key,
                    estimated_microusd,
                    request.billing_month,
                    request.now,
                    request.now,
                ),
            )
            outcome = (
                QueueOutcome.CREATED_WITH_WARNING
                if decision.status is ReservationStatus.WARNING
                else QueueOutcome.CREATED
            )
            if cursor.lastrowid is None:
                raise QueueError("SQLite did not return an inserted job ID")
            return EnqueueResult(
                outcome=outcome,
                job_id=int(cursor.lastrowid),
                decision=decision,
            )

    def lease_next(
        self,
        *,
        worker_id: str,
        now: str,
        lease_expires_at: str,
    ) -> JobLease | None:
        """Recover safe expired leases and lease one ready job."""

        if not worker_id.strip():
            raise ValueError("worker_id must not be blank")
        if lease_expires_at <= now:
            raise ValueError("lease_expires_at must be later than now")

        with immediate_transaction(self._connection):
            self._connection.execute(
                """
                UPDATE jobs
                SET state = 'QUEUED',
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    updated_at = ?
                WHERE state = 'LEASED' AND lease_expires_at <= ?
                """,
                (now, now),
            )
            row = self._connection.execute(
                """
                SELECT id, kind, draft_id, input_version, attempts, estimated_cost_microusd
                FROM jobs
                WHERE state = 'QUEUED' AND next_attempt_at <= ?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM job_dependencies
                      JOIN jobs AS prerequisite
                        ON prerequisite.id = job_dependencies.prerequisite_job_id
                      WHERE job_dependencies.job_id = jobs.id
                        AND prerequisite.state != 'SUCCEEDED'
                  )
                ORDER BY next_attempt_at, id
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                return None
            job_id = int(row["id"])
            cursor = self._connection.execute(
                """
                UPDATE jobs
                SET state = 'LEASED',
                    attempts = attempts + 1,
                    lease_owner = ?,
                    lease_expires_at = ?,
                    updated_at = ?
                WHERE id = ? AND state = 'QUEUED'
                """,
                (worker_id, lease_expires_at, now, job_id),
            )
            if cursor.rowcount != 1:
                raise QueueError("ready job changed during lease transaction")
            return JobLease(
                job_id=job_id,
                kind=str(row["kind"]),
                draft_id=(str(row["draft_id"]) if row["draft_id"] is not None else None),
                input_version=int(row["input_version"]),
                attempts=int(row["attempts"]) + 1,
                estimated_cost_usd=_microusd_to_usd(
                    int(row["estimated_cost_microusd"])
                ),
            )

    def add_dependency(
        self,
        *,
        job_id: int,
        prerequisite_job_id: int,
        now: str,
    ) -> None:
        """Keep queued work unleaseable until one prerequisite succeeds."""

        if job_id <= 0 or prerequisite_job_id <= 0 or job_id == prerequisite_job_id:
            raise ValueError("job dependency identifiers are invalid")
        if not now.strip():
            raise ValueError("now must not be blank")
        with immediate_transaction(self._connection):
            self._connection.execute(
                """
                INSERT INTO job_dependencies (job_id, prerequisite_job_id, created_at)
                VALUES (?, ?, ?)
                """,
                (job_id, prerequisite_job_id, now),
            )

    def cancel_queued(
        self,
        *,
        job_id: int,
        error_category: str,
        now: str,
    ) -> None:
        """Release one dependent reservation that never crossed its call boundary."""

        if job_id <= 0:
            raise ValueError("job_id must be positive")
        if not error_category.strip() or not now.strip():
            raise ValueError("error_category and now must not be blank")
        with immediate_transaction(self._connection):
            row = self._connection.execute(
                "SELECT state FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise QueueError("job does not exist")
            if str(row["state"]) == "CANCELLED":
                return
            cursor = self._connection.execute(
                """
                UPDATE jobs
                SET state = 'CANCELLED', error_category = ?, updated_at = ?
                WHERE id = ? AND state = 'QUEUED'
                """,
                (error_category, now, job_id),
            )
            if cursor.rowcount != 1:
                raise QueueError("only queued work can be cancelled before a call")

    def mark_external_call_started(
        self,
        *,
        job_id: int,
        worker_id: str,
        now: str,
    ) -> None:
        """Prevent automatic retry before crossing an external side-effect boundary."""

        with immediate_transaction(self._connection):
            cursor = self._connection.execute(
                """
                UPDATE jobs
                SET state = 'RESULT_UNKNOWN', updated_at = ?
                WHERE id = ? AND state = 'LEASED' AND lease_owner = ?
                """,
                (now, job_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise QueueError("job is not leased by this worker")

    def fail_before_external_call(
        self,
        *,
        job_id: int,
        worker_id: str,
        error_category: str,
        now: str,
    ) -> None:
        """Release a reservation when local validation fails before any provider call."""

        if not error_category.strip():
            raise ValueError("error_category must not be blank")
        with immediate_transaction(self._connection):
            cursor = self._connection.execute(
                """
                UPDATE jobs
                SET state = 'FAILED', lease_owner = NULL, lease_expires_at = NULL,
                    error_category = ?, updated_at = ?
                WHERE id = ? AND state = 'LEASED' AND lease_owner = ?
                """,
                (error_category, now, job_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise QueueError("job is not leased by this worker")

    def finalize_succeeded(
        self,
        *,
        job_id: int,
        worker_id: str,
        usage: UsageMetadata,
        actual_cost_usd: Decimal,
    ) -> None:
        """Atomically replace a reservation with measured billed usage."""

        actual_cost_microusd = _usd_to_microusd(actual_cost_usd)
        if actual_cost_microusd < 0:
            raise ValueError("actual_cost_usd must not be negative")
        with immediate_transaction(self._connection):
            row = self._load_job_for_finalization(job_id=job_id)
            if self._is_already_finalized(
                job_id=job_id,
                job_state=str(row["state"]),
                expected_job_state="SUCCEEDED",
                expected_billing_status="ACTUAL",
            ):
                return
            self._require_external_call_owner(row=row, worker_id=worker_id)
            self._insert_usage(
                job_id=job_id,
                draft_id=_optional_text(row["draft_id"]),
                billing_month=str(row["billing_month"]),
                estimated_cost_microusd=int(row["estimated_cost_microusd"]),
                actual_cost_microusd=actual_cost_microusd,
                billing_status="ACTUAL",
                usage=usage,
            )
            self._connection.execute(
                """
                UPDATE jobs
                SET state = 'SUCCEEDED', lease_owner = NULL, lease_expires_at = NULL,
                    error_category = NULL, updated_at = ?
                WHERE id = ?
                """,
                (usage.now, job_id),
            )

    def finalize_unknown_billed(
        self,
        *,
        job_id: int,
        worker_id: str,
        error_category: str,
        usage: UsageMetadata,
    ) -> None:
        """Persist a conservative charge without making an ambiguous call retryable."""

        if not error_category.strip():
            raise ValueError("error_category must not be blank")
        with immediate_transaction(self._connection):
            row = self._load_job_for_finalization(job_id=job_id)
            if self._is_already_finalized(
                job_id=job_id,
                job_state=str(row["state"]),
                expected_job_state="RESULT_UNKNOWN",
                expected_billing_status="UNKNOWN_BILLED",
            ):
                return
            self._require_external_call_owner(row=row, worker_id=worker_id)
            self._insert_usage(
                job_id=job_id,
                draft_id=_optional_text(row["draft_id"]),
                billing_month=str(row["billing_month"]),
                estimated_cost_microusd=int(row["estimated_cost_microusd"]),
                actual_cost_microusd=None,
                billing_status="UNKNOWN_BILLED",
                usage=usage,
            )
            self._connection.execute(
                """
                UPDATE jobs
                SET lease_owner = NULL, lease_expires_at = NULL,
                    error_category = ?, updated_at = ?
                WHERE id = ?
                """,
                (error_category, usage.now, job_id),
            )

    def _load_job_for_finalization(self, *, job_id: int) -> sqlite3.Row:
        row = cast(
            sqlite3.Row | None,
            self._connection.execute(
                """
                SELECT id, draft_id, state, lease_owner,
                       estimated_cost_microusd, billing_month
                FROM jobs
                WHERE id = ?
                """,
                (job_id,),
            ).fetchone(),
        )
        if row is None:
            raise QueueError("job does not exist")
        return row

    def _is_already_finalized(
        self,
        *,
        job_id: int,
        job_state: str,
        expected_job_state: str,
        expected_billing_status: str,
    ) -> bool:
        usage = self._connection.execute(
            "SELECT billing_status FROM api_usage WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if usage is None:
            return False
        if (
            job_state == expected_job_state
            and str(usage["billing_status"]) == expected_billing_status
        ):
            return True
        raise QueueError("job already has a conflicting usage finalization")

    @staticmethod
    def _require_external_call_owner(*, row: sqlite3.Row, worker_id: str) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id must not be blank")
        if str(row["state"]) != "RESULT_UNKNOWN" or row["lease_owner"] != worker_id:
            raise QueueError("external call is not owned by this worker")

    def _insert_usage(
        self,
        *,
        job_id: int,
        draft_id: str | None,
        billing_month: str,
        estimated_cost_microusd: int,
        actual_cost_microusd: int | None,
        billing_status: str,
        usage: UsageMetadata,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO api_usage (
                draft_id, job_id, stage, provider_model, input_units, output_units,
                duration_seconds, image_quality, actual_cost_microusd,
                estimated_cost_microusd, price_version, billing_status,
                billing_month, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                draft_id,
                job_id,
                usage.stage,
                usage.provider_model,
                usage.input_units,
                usage.output_units,
                str(usage.duration_seconds) if usage.duration_seconds is not None else None,
                usage.image_quality,
                actual_cost_microusd,
                estimated_cost_microusd,
                usage.price_version,
                billing_status,
                billing_month,
                usage.now,
            ),
        )

    def _spend_snapshot(self, *, draft_id: str, billing_month: str) -> SpendSnapshot:
        post_actual = self._connection.execute(
            """
            SELECT COALESCE(SUM(COALESCE(actual_cost_microusd, estimated_cost_microusd)), 0)
            FROM api_usage
            WHERE draft_id = ?
            """,
            (draft_id,),
        ).fetchone()[0]
        month_actual = self._connection.execute(
            """
            SELECT COALESCE(SUM(COALESCE(actual_cost_microusd, estimated_cost_microusd)), 0)
            FROM api_usage
            WHERE billing_month = ?
            """,
            (billing_month,),
        ).fetchone()[0]
        post_reserved = self._connection.execute(
            """
            SELECT COALESCE(SUM(estimated_cost_microusd), 0)
            FROM jobs
            WHERE draft_id = ? AND state IN ('QUEUED', 'LEASED', 'RESULT_UNKNOWN')
              AND NOT EXISTS (
                  SELECT 1 FROM api_usage WHERE api_usage.job_id = jobs.id
              )
            """,
            (draft_id,),
        ).fetchone()[0]
        month_reserved = self._connection.execute(
            """
            SELECT COALESCE(SUM(estimated_cost_microusd), 0)
            FROM jobs
            WHERE billing_month = ? AND state IN ('QUEUED', 'LEASED', 'RESULT_UNKNOWN')
              AND NOT EXISTS (
                  SELECT 1 FROM api_usage WHERE api_usage.job_id = jobs.id
              )
            """,
            (billing_month,),
        ).fetchone()[0]
        return SpendSnapshot(
            post_actual_usd=_microusd_to_usd(int(post_actual)),
            post_reserved_usd=_microusd_to_usd(int(post_reserved)),
            month_actual_usd=_microusd_to_usd(int(month_actual)),
            month_reserved_usd=_microusd_to_usd(int(month_reserved)),
        )

def _usd_to_microusd(value: Decimal) -> int:
    microusd = value * _MICRODOLLARS_PER_DOLLAR
    if microusd != microusd.to_integral_value():
        raise ValueError("USD amount supports at most six decimal places")
    return int(microusd)


def _microusd_to_usd(value: int) -> Decimal:
    return Decimal(value) / _MICRODOLLARS_PER_DOLLAR


def _optional_text(value: object) -> str | None:
    return str(value) if value is not None else None
