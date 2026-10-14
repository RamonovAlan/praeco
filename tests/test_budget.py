from decimal import Decimal

import pytest

from praeco.budget import BudgetPolicy, ReservationStatus, SpendSnapshot


def confirmed_policy() -> BudgetPolicy:
    return BudgetPolicy.from_mapping(
        {
            "PRAECO_PER_POST_WARNING_USD": "0.50",
            "PRAECO_PER_POST_HARD_LIMIT_USD": "0.75",
            "PRAECO_MONTHLY_BUDGET_USD": "25",
            "PRAECO_TEXT_DRAFTS_PER_REQUEST": "1",
            "PRAECO_IMAGE_OUTPUTS_PER_REQUEST": "1",
            "PRAECO_AUTOMATIC_PAID_RETRY_LIMIT": "0",
        }
    )


def test_missing_limits_fail_closed_for_paid_calls() -> None:
    policy = BudgetPolicy.from_mapping({})

    decision = policy.assess_reservation(
        snapshot=SpendSnapshot(),
        estimated_call_usd=Decimal("0.01"),
    )

    assert decision.status is ReservationStatus.HARD_STOP
    assert not decision.allowed


def test_reserved_jobs_count_toward_the_per_post_warning() -> None:
    policy = confirmed_policy()

    decision = policy.assess_reservation(
        snapshot=SpendSnapshot(
            post_actual_usd=Decimal("0.20"),
            post_reserved_usd=Decimal("0.20"),
        ),
        estimated_call_usd=Decimal("0.10"),
    )

    assert decision.status is ReservationStatus.WARNING
    assert decision.allowed
    assert decision.projected_post_usd == Decimal("0.50")


def test_per_post_hard_limit_blocks_the_new_reservation() -> None:
    policy = confirmed_policy()

    decision = policy.assess_reservation(
        snapshot=SpendSnapshot(post_actual_usd=Decimal("0.70")),
        estimated_call_usd=Decimal("0.06"),
    )

    assert decision.status is ReservationStatus.HARD_STOP
    assert not decision.allowed


def test_monthly_reserved_jobs_count_toward_the_hard_limit() -> None:
    policy = confirmed_policy()

    decision = policy.assess_reservation(
        snapshot=SpendSnapshot(
            month_actual_usd=Decimal("24.60"),
            month_reserved_usd=Decimal("0.30"),
        ),
        estimated_call_usd=Decimal("0.11"),
    )

    assert decision.status is ReservationStatus.HARD_STOP
    assert decision.reason == "monthly hard limit would be exceeded"


def test_confirmed_policy_uses_single_outputs_and_no_paid_auto_retry() -> None:
    policy = confirmed_policy()

    assert policy.text_drafts_per_request == 1
    assert policy.image_outputs_per_request == 1
    assert policy.automatic_paid_retry_limit == 0


def test_partial_budget_configuration_is_rejected() -> None:
    with pytest.raises(ValueError, match="all zero or all positive"):
        BudgetPolicy.from_mapping({"PRAECO_MONTHLY_BUDGET_USD": "25"})
