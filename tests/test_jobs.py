from decimal import Decimal
from pathlib import Path

import pytest

from praeco.budget import BudgetPolicy
from praeco.jobs import (
    JobRepository,
    PaidJobRequest,
    QueueError,
    QueueOutcome,
    UsageMetadata,
)
from praeco.storage import apply_migrations, connect_database

MIGRATIONS = Path(__file__).parents[1] / "migrations"


def policy() -> BudgetPolicy:
    return BudgetPolicy(
        per_post_warning_usd=Decimal("0.50"),
        per_post_hard_limit_usd=Decimal("0.75"),
        monthly_hard_limit_usd=Decimal("25"),
    )


def repository(tmp_path: Path) -> JobRepository:
    connection = connect_database(tmp_path / "praeco.sqlite3")
    apply_migrations(connection, MIGRATIONS)
    for draft_id in ("draft-1", "draft-2"):
        connection.execute(
            """
            INSERT INTO drafts (
                id, owner_id, source_kind, source_text, language, state, created_at, updated_at
            ) VALUES (?, 101, 'text', 'thought', 'ru', 'RECEIVED', 'now', 'now')
            """,
            (draft_id,),
        )
    connection.commit()
    return JobRepository(connection)


def request(
    *,
    deduplication_key: str,
    cost: str,
    draft_id: str = "draft-1",
) -> PaidJobRequest:
    return PaidJobRequest(
        kind="generate_image",
        draft_id=draft_id,
        input_version=1,
        deduplication_key=deduplication_key,
        estimated_cost_usd=Decimal(cost),
        billing_month="2026-10",
        now="2026-10-12T12:00:00Z",
    )


def usage(*, now: str = "2026-10-12T12:00:03Z") -> UsageMetadata:
    return UsageMetadata(
        stage="image_generation",
        provider_model="image-model",
        price_version="2026-10-01",
        image_quality="medium",
        now=now,
    )


def start_external_call(jobs: JobRepository, *, deduplication_key: str) -> int:
    created = jobs.enqueue_paid(
        request=request(deduplication_key=deduplication_key, cost="0.20"),
        policy=policy(),
    )
    assert created.job_id is not None
    lease = jobs.lease_next(
        worker_id="worker-a",
        now="2026-10-12T12:00:01Z",
        lease_expires_at="2026-10-12T12:05:00Z",
    )
    assert lease is not None
    jobs.mark_external_call_started(
        job_id=lease.job_id,
        worker_id="worker-a",
        now="2026-10-12T12:00:02Z",
    )
    return lease.job_id


def test_duplicate_enqueue_returns_the_existing_job_without_new_reservation(
    tmp_path: Path,
) -> None:
    jobs = repository(tmp_path)

    first = jobs.enqueue_paid(
        request=request(deduplication_key="image:draft-1:v1", cost="0.20"),
        policy=policy(),
    )
    duplicate = jobs.enqueue_paid(
        request=request(deduplication_key="image:draft-1:v1", cost="0.20"),
        policy=policy(),
    )

    assert first.outcome is QueueOutcome.CREATED
    assert duplicate.outcome is QueueOutcome.DUPLICATE
    assert duplicate.job_id == first.job_id


def test_queued_reservations_trigger_warning_and_hard_stop(tmp_path: Path) -> None:
    jobs = repository(tmp_path)

    first = jobs.enqueue_paid(
        request=request(deduplication_key="job-1", cost="0.40"),
        policy=policy(),
    )
    warning = jobs.enqueue_paid(
        request=request(deduplication_key="job-2", cost="0.10"),
        policy=policy(),
    )
    blocked = jobs.enqueue_paid(
        request=request(deduplication_key="job-3", cost="0.26"),
        policy=policy(),
    )

    assert first.outcome is QueueOutcome.CREATED
    assert warning.outcome is QueueOutcome.CREATED_WITH_WARNING
    assert blocked.outcome is QueueOutcome.BUDGET_BLOCKED
    assert blocked.job_id is None


def test_monthly_reservations_include_other_drafts(tmp_path: Path) -> None:
    jobs = repository(tmp_path)
    small_month = BudgetPolicy(
        per_post_warning_usd=Decimal("0.50"),
        per_post_hard_limit_usd=Decimal("0.75"),
        monthly_hard_limit_usd=Decimal("1.00"),
    )

    jobs.enqueue_paid(
        request=request(deduplication_key="draft-1-job", cost="0.60"),
        policy=small_month,
    )
    blocked = jobs.enqueue_paid(
        request=request(
            deduplication_key="draft-2-job",
            cost="0.50",
            draft_id="draft-2",
        ),
        policy=small_month,
    )

    assert blocked.outcome is QueueOutcome.BUDGET_BLOCKED
    assert blocked.decision is not None
    assert blocked.decision.reason == "monthly hard limit would be exceeded"


def test_expired_pre_call_lease_is_recovered(tmp_path: Path) -> None:
    jobs = repository(tmp_path)
    created = jobs.enqueue_paid(
        request=request(deduplication_key="recoverable", cost="0.10"),
        policy=policy(),
    )

    first_lease = jobs.lease_next(
        worker_id="worker-a",
        now="2026-10-12T12:00:01Z",
        lease_expires_at="2026-10-12T12:05:00Z",
    )
    recovered_lease = jobs.lease_next(
        worker_id="worker-b",
        now="2026-10-12T12:05:01Z",
        lease_expires_at="2026-10-12T12:10:00Z",
    )

    assert first_lease is not None
    assert recovered_lease is not None
    assert first_lease.job_id == created.job_id == recovered_lease.job_id
    assert recovered_lease.attempts == 2


def test_external_call_boundary_never_auto_requeues(tmp_path: Path) -> None:
    jobs = repository(tmp_path)
    jobs.enqueue_paid(
        request=request(deduplication_key="unknown-result", cost="0.10"),
        policy=policy(),
    )
    lease = jobs.lease_next(
        worker_id="worker-a",
        now="2026-10-12T12:00:01Z",
        lease_expires_at="2026-10-12T12:05:00Z",
    )
    assert lease is not None

    jobs.mark_external_call_started(
        job_id=lease.job_id,
        worker_id="worker-a",
        now="2026-10-12T12:00:02Z",
    )
    later_lease = jobs.lease_next(
        worker_id="worker-b",
        now="2026-10-12T13:00:00Z",
        lease_expires_at="2026-10-12T13:05:00Z",
    )
    blocked_by_unknown_reservation = jobs.enqueue_paid(
        request=request(deduplication_key="after-unknown", cost="0.66"),
        policy=policy(),
    )

    assert later_lease is None
    assert blocked_by_unknown_reservation.outcome is QueueOutcome.BUDGET_BLOCKED


def test_success_replaces_reservation_with_actual_usage_atomically(tmp_path: Path) -> None:
    jobs = repository(tmp_path)
    job_id = start_external_call(jobs, deduplication_key="successful")

    jobs.finalize_succeeded(
        job_id=job_id,
        worker_id="worker-a",
        usage=usage(),
        actual_cost_usd=Decimal("0.18"),
    )
    jobs.finalize_succeeded(
        job_id=job_id,
        worker_id="worker-a",
        usage=usage(),
        actual_cost_usd=Decimal("0.18"),
    )

    verification = connect_database(tmp_path / "praeco.sqlite3")
    job = verification.execute(
        "SELECT state, lease_owner FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    recorded = verification.execute(
        """
        SELECT billing_status, actual_cost_microusd, estimated_cost_microusd
        FROM api_usage WHERE job_id = ?
        """,
        (job_id,),
    ).fetchall()
    assert job is not None
    assert job["state"] == "SUCCEEDED"
    assert job["lease_owner"] is None
    assert len(recorded) == 1
    assert recorded[0]["billing_status"] == "ACTUAL"
    assert recorded[0]["actual_cost_microusd"] == 180_000
    assert recorded[0]["estimated_cost_microusd"] == 200_000

    follow_up = jobs.enqueue_paid(
        request=request(deduplication_key="after-success", cost="0.57"),
        policy=policy(),
    )
    assert follow_up.outcome is QueueOutcome.CREATED_WITH_WARNING


def test_unknown_billed_cost_is_counted_once_and_never_requeued(tmp_path: Path) -> None:
    jobs = repository(tmp_path)
    job_id = start_external_call(jobs, deduplication_key="timed-out")

    jobs.finalize_unknown_billed(
        job_id=job_id,
        worker_id="worker-a",
        error_category="provider_timeout",
        usage=usage(),
    )
    jobs.finalize_unknown_billed(
        job_id=job_id,
        worker_id="worker-a",
        error_category="provider_timeout",
        usage=usage(),
    )

    verification = connect_database(tmp_path / "praeco.sqlite3")
    job = verification.execute(
        "SELECT state, lease_owner, error_category FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    recorded = verification.execute(
        "SELECT billing_status, actual_cost_microusd FROM api_usage WHERE job_id = ?",
        (job_id,),
    ).fetchall()
    assert job is not None
    assert job["state"] == "RESULT_UNKNOWN"
    assert job["lease_owner"] is None
    assert job["error_category"] == "provider_timeout"
    assert len(recorded) == 1
    assert recorded[0]["billing_status"] == "UNKNOWN_BILLED"
    assert recorded[0]["actual_cost_microusd"] is None

    follow_up = jobs.enqueue_paid(
        request=request(deduplication_key="after-timeout", cost="0.55"),
        policy=policy(),
    )
    assert follow_up.outcome is QueueOutcome.CREATED_WITH_WARNING
    assert jobs.lease_next(
        worker_id="worker-b",
        now="2026-10-12T13:00:00Z",
        lease_expires_at="2026-10-12T13:05:00Z",
    ) is not None


def test_only_external_call_owner_can_finalize_usage(tmp_path: Path) -> None:
    jobs = repository(tmp_path)
    job_id = start_external_call(jobs, deduplication_key="owned")

    with pytest.raises(QueueError, match="owned"):
        jobs.finalize_succeeded(
            job_id=job_id,
            worker_id="worker-b",
            usage=usage(),
            actual_cost_usd=Decimal("0.18"),
        )

    verification = connect_database(tmp_path / "praeco.sqlite3")
    assert verification.execute(
        "SELECT COUNT(*) FROM api_usage WHERE job_id = ?", (job_id,)
    ).fetchone()[0] == 0


def test_dependent_job_waits_for_successful_prerequisite(tmp_path: Path) -> None:
    jobs = repository(tmp_path)
    prerequisite = jobs.enqueue_paid(
        request=request(deduplication_key="transcription", cost="0.01"),
        policy=policy(),
    )
    dependent = jobs.enqueue_paid(
        request=request(deduplication_key="revision", cost="0.20"),
        policy=policy(),
    )
    assert prerequisite.job_id is not None
    assert dependent.job_id is not None
    jobs.add_dependency(
        job_id=dependent.job_id,
        prerequisite_job_id=prerequisite.job_id,
        now="2026-10-12T12:00:00Z",
    )

    first = jobs.lease_next(
        worker_id="worker-a",
        now="2026-10-12T12:00:01Z",
        lease_expires_at="2026-10-12T12:05:00Z",
    )
    blocked = jobs.lease_next(
        worker_id="worker-b",
        now="2026-10-12T12:00:02Z",
        lease_expires_at="2026-10-12T12:05:00Z",
    )
    assert first is not None
    assert first.job_id == prerequisite.job_id
    assert blocked is None

    jobs.mark_external_call_started(
        job_id=first.job_id,
        worker_id="worker-a",
        now="2026-10-12T12:00:03Z",
    )
    jobs.finalize_succeeded(
        job_id=first.job_id,
        worker_id="worker-a",
        usage=usage(now="2026-10-12T12:00:04Z"),
        actual_cost_usd=Decimal("0.01"),
    )
    released = jobs.lease_next(
        worker_id="worker-b",
        now="2026-10-12T12:00:05Z",
        lease_expires_at="2026-10-12T12:05:00Z",
    )

    assert released is not None
    assert released.job_id == dependent.job_id
