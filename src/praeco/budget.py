"""Fail-closed spending checks for paid provider jobs."""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum


class ReservationStatus(StrEnum):
    """Result of evaluating an estimated paid call against current usage."""

    ALLOWED = "ALLOWED"
    WARNING = "WARNING"
    HARD_STOP = "HARD_STOP"


@dataclass(frozen=True, slots=True)
class SpendSnapshot:
    """Actual and reserved spend known before scheduling another paid call."""

    post_actual_usd: Decimal = Decimal("0")
    post_reserved_usd: Decimal = Decimal("0")
    month_actual_usd: Decimal = Decimal("0")
    month_reserved_usd: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        for field_name in (
            "post_actual_usd",
            "post_reserved_usd",
            "month_actual_usd",
            "month_reserved_usd",
        ):
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} must not be negative")


@dataclass(frozen=True, slots=True)
class ReservationDecision:
    """A deterministic decision that can be persisted with a queued job."""

    status: ReservationStatus
    estimated_call_usd: Decimal
    projected_post_usd: Decimal
    projected_month_usd: Decimal
    reason: str

    @property
    def allowed(self) -> bool:
        """Return whether the caller may reserve and enqueue the paid job."""

        return self.status is not ReservationStatus.HARD_STOP


@dataclass(frozen=True, slots=True)
class BudgetPolicy:
    """Owner-approved USD limits and conservative generation defaults."""

    per_post_warning_usd: Decimal
    per_post_hard_limit_usd: Decimal
    monthly_hard_limit_usd: Decimal
    text_drafts_per_request: int = 1
    image_outputs_per_request: int = 1
    automatic_paid_retry_limit: int = 0

    def __post_init__(self) -> None:
        limits = (
            self.per_post_warning_usd,
            self.per_post_hard_limit_usd,
            self.monthly_hard_limit_usd,
        )
        if any(limit < 0 for limit in limits):
            raise ValueError("budget limits must not be negative")
        if any(limit == 0 for limit in limits) and any(limit != 0 for limit in limits):
            raise ValueError("budget limits must be all zero or all positive")
        if limits != (Decimal("0"), Decimal("0"), Decimal("0")):
            if self.per_post_warning_usd > self.per_post_hard_limit_usd:
                raise ValueError("per-post warning must not exceed the hard limit")
            if self.per_post_hard_limit_usd > self.monthly_hard_limit_usd:
                raise ValueError("per-post hard limit must not exceed the monthly limit")
        if self.text_drafts_per_request != 1:
            raise ValueError("MVP must create exactly one text draft per request")
        if self.image_outputs_per_request != 1:
            raise ValueError("MVP must create exactly one image per request")
        if self.automatic_paid_retry_limit < 0:
            raise ValueError("automatic paid retry limit must not be negative")

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "BudgetPolicy":
        """Load a policy from environment-like values without reading secrets."""

        return cls(
            per_post_warning_usd=_decimal_value(
                values, "PRAECO_PER_POST_WARNING_USD", default="0"
            ),
            per_post_hard_limit_usd=_decimal_value(
                values, "PRAECO_PER_POST_HARD_LIMIT_USD", default="0"
            ),
            monthly_hard_limit_usd=_decimal_value(
                values, "PRAECO_MONTHLY_BUDGET_USD", default="0"
            ),
            text_drafts_per_request=_integer_value(
                values, "PRAECO_TEXT_DRAFTS_PER_REQUEST", default="1"
            ),
            image_outputs_per_request=_integer_value(
                values, "PRAECO_IMAGE_OUTPUTS_PER_REQUEST", default="1"
            ),
            automatic_paid_retry_limit=_integer_value(
                values, "PRAECO_AUTOMATIC_PAID_RETRY_LIMIT", default="0"
            ),
        )

    @property
    def paid_calls_enabled(self) -> bool:
        """Return false until a complete positive limit set is installed."""

        return self.per_post_hard_limit_usd > 0

    def assess_reservation(
        self,
        *,
        snapshot: SpendSnapshot,
        estimated_call_usd: Decimal,
    ) -> ReservationDecision:
        """Check a conservative call estimate against actual and reserved spend."""

        if estimated_call_usd < 0:
            raise ValueError("estimated_call_usd must not be negative")

        projected_post = (
            snapshot.post_actual_usd + snapshot.post_reserved_usd + estimated_call_usd
        )
        projected_month = (
            snapshot.month_actual_usd + snapshot.month_reserved_usd + estimated_call_usd
        )

        if not self.paid_calls_enabled and estimated_call_usd > 0:
            return ReservationDecision(
                status=ReservationStatus.HARD_STOP,
                estimated_call_usd=estimated_call_usd,
                projected_post_usd=projected_post,
                projected_month_usd=projected_month,
                reason="paid calls are disabled until all budget limits are configured",
            )
        if projected_month > self.monthly_hard_limit_usd:
            return ReservationDecision(
                status=ReservationStatus.HARD_STOP,
                estimated_call_usd=estimated_call_usd,
                projected_post_usd=projected_post,
                projected_month_usd=projected_month,
                reason="monthly hard limit would be exceeded",
            )
        if projected_post > self.per_post_hard_limit_usd:
            return ReservationDecision(
                status=ReservationStatus.HARD_STOP,
                estimated_call_usd=estimated_call_usd,
                projected_post_usd=projected_post,
                projected_month_usd=projected_month,
                reason="per-post hard limit would be exceeded",
            )
        if projected_post >= self.per_post_warning_usd:
            return ReservationDecision(
                status=ReservationStatus.WARNING,
                estimated_call_usd=estimated_call_usd,
                projected_post_usd=projected_post,
                projected_month_usd=projected_month,
                reason="per-post warning threshold reached",
            )
        return ReservationDecision(
            status=ReservationStatus.ALLOWED,
            estimated_call_usd=estimated_call_usd,
            projected_post_usd=projected_post,
            projected_month_usd=projected_month,
            reason="reservation is within configured limits",
        )


def _decimal_value(values: Mapping[str, str], key: str, *, default: str) -> Decimal:
    raw_value = values.get(key, default).strip()
    try:
        value = Decimal(raw_value)
    except InvalidOperation as error:
        raise ValueError(f"{key} must be a decimal number") from error
    if not value.is_finite():
        raise ValueError(f"{key} must be finite")
    return value


def _integer_value(values: Mapping[str, str], key: str, *, default: str) -> int:
    raw_value = values.get(key, default).strip()
    try:
        return int(raw_value)
    except ValueError as error:
        raise ValueError(f"{key} must be an integer") from error
