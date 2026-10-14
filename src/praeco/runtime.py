"""Bounded single-process lifecycle for polling, provider work, and delivery."""

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from praeco.outbox import DeliveryOutcome, DeliveryRunResult
from praeco.publication import PublicationWorkerOutcome, PublicationWorkerResult
from praeco.telegram_api import PollingResult
from praeco.worker import WorkerOutcome, WorkerResult


class TelegramPoller(Protocol):
    """Fetch and dispatch at most one bounded Telegram update batch."""

    def poll_once(
        self,
        *,
        offset: int | None,
        timeout_seconds: int,
        now: str,
        billing_month: str,
    ) -> PollingResult: ...


class ProviderJobWorker(Protocol):
    """Execute at most one durable provider job."""

    def run_once(
        self,
        *,
        worker_id: str,
        now: str,
        lease_expires_at: str,
    ) -> WorkerResult: ...


class ReviewDeliveryWorker(Protocol):
    """Attempt at most one durable Telegram review delivery."""

    def run_once(
        self,
        *,
        worker_id: str,
        now: str,
        lease_expires_at: str,
        retry_at: str,
    ) -> DeliveryRunResult: ...


class LinkedInPublicationWorker(Protocol):
    """Execute at most one explicit publication request."""

    def run_once(self, *, now: str) -> PublicationWorkerResult: ...


class StopSignal(Protocol):
    """Cooperative shutdown signal compatible with threading.Event."""

    def is_set(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """Bounds that keep each lifecycle iteration finite and stoppable."""

    polling_timeout_seconds: int = 30
    lease_seconds: int = 300
    delivery_retry_seconds: int = 300
    max_provider_jobs_per_tick: int = 10
    max_deliveries_per_tick: int = 10
    idle_sleep_seconds: float = 1.0
    provider_worker_id: str = "provider-worker"
    delivery_worker_id: str = "delivery-worker"

    def __post_init__(self) -> None:
        if not 1 <= self.polling_timeout_seconds <= 50:
            raise ValueError("polling_timeout_seconds must be between 1 and 50")
        for field_name in (
            "lease_seconds",
            "delivery_retry_seconds",
            "max_provider_jobs_per_tick",
            "max_deliveries_per_tick",
        ):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be positive")
        if self.idle_sleep_seconds < 0:
            raise ValueError("idle_sleep_seconds must not be negative")
        for field_name in ("provider_worker_id", "delivery_worker_id"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} must not be blank")


@dataclass(frozen=True, slots=True)
class RuntimeTickResult:
    """Non-sensitive summary of one complete bounded application iteration."""

    polling: PollingResult
    provider_results: tuple[WorkerResult, ...]
    delivery_results: tuple[DeliveryRunResult, ...]
    publication_results: tuple[PublicationWorkerResult, ...]
    provider_limit_reached: bool
    delivery_limit_reached: bool

    @property
    def had_activity(self) -> bool:
        """Return whether the iteration received or completed any work."""

        return bool(
            self.polling.received
            or self.provider_results
            or self.delivery_results
            or self.publication_results
        )


class PraecoRuntime:
    """Run one poller and the two durable queues in a bounded single process."""

    def __init__(
        self,
        *,
        poller: TelegramPoller,
        provider_worker: ProviderJobWorker,
        delivery_worker: ReviewDeliveryWorker,
        publication_worker: LinkedInPublicationWorker | None = None,
        config: RuntimeConfig | None = None,
        clock: Callable[[], datetime] | None = None,
        idle_wait: Callable[[float], None] | None = None,
    ) -> None:
        self._poller = poller
        self._provider_worker = provider_worker
        self._delivery_worker = delivery_worker
        self._publication_worker = publication_worker
        self._config = config or RuntimeConfig()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._idle_wait = idle_wait or time.sleep
        self._offset: int | None = None

    @property
    def next_offset(self) -> int | None:
        """Expose the in-memory Telegram offset for health and tests."""

        return self._offset

    def run_tick(self) -> RuntimeTickResult:
        """Poll once, then drain bounded provider and delivery work."""

        now_value = self._clock()
        if now_value.tzinfo is None or now_value.utcoffset() is None:
            raise ValueError("runtime clock must return a timezone-aware datetime")
        now_value = now_value.astimezone(UTC)
        now = _timestamp(now_value)
        lease_expires_at = _timestamp(
            now_value + timedelta(seconds=self._config.lease_seconds)
        )
        retry_at = _timestamp(
            now_value + timedelta(seconds=self._config.delivery_retry_seconds)
        )
        polling = self._poller.poll_once(
            offset=self._offset,
            timeout_seconds=self._config.polling_timeout_seconds,
            now=now,
            billing_month=now_value.strftime("%Y-%m"),
        )
        self._offset = polling.next_offset

        publication_results: tuple[PublicationWorkerResult, ...] = ()
        if self._publication_worker is not None:
            publication_result = self._publication_worker.run_once(now=now)
            if publication_result.outcome is not PublicationWorkerOutcome.IDLE:
                publication_results = (publication_result,)

        (
            provider_results,
            delivery_results,
            provider_limit_reached,
            delivery_limit_reached,
        ) = self._drain_provider_jobs_and_deliveries(
            now=now,
            lease_expires_at=lease_expires_at,
            retry_at=retry_at,
        )
        return RuntimeTickResult(
            polling=polling,
            provider_results=provider_results,
            delivery_results=delivery_results,
            publication_results=publication_results,
            provider_limit_reached=provider_limit_reached,
            delivery_limit_reached=delivery_limit_reached,
        )

    def run_until_stopped(self, stop_signal: StopSignal) -> int:
        """Run bounded ticks until cooperative shutdown is requested."""

        ticks = 0
        while not stop_signal.is_set():
            result = self.run_tick()
            ticks += 1
            if (
                not result.had_activity
                and not stop_signal.is_set()
                and self._config.idle_sleep_seconds > 0
            ):
                self._idle_wait(self._config.idle_sleep_seconds)
        return ticks

    def _drain_provider_jobs_and_deliveries(
        self,
        *,
        now: str,
        lease_expires_at: str,
        retry_at: str,
    ) -> tuple[
        tuple[WorkerResult, ...],
        tuple[DeliveryRunResult, ...],
        bool,
        bool,
    ]:
        provider_results: list[WorkerResult] = []
        delivery_results: list[DeliveryRunResult] = []
        provider_limit_reached = True
        for _ in range(self._config.max_provider_jobs_per_tick):
            result = self._provider_worker.run_once(
                worker_id=self._config.provider_worker_id,
                now=now,
                lease_expires_at=lease_expires_at,
            )
            if result.outcome is WorkerOutcome.IDLE:
                provider_limit_reached = False
                break
            provider_results.append(result)
            delivered, _ = self._drain_deliveries(
                now=now,
                lease_expires_at=lease_expires_at,
                retry_at=retry_at,
                limit=self._config.max_deliveries_per_tick - len(delivery_results),
            )
            delivery_results.extend(delivered)
        delivered, delivery_limit_reached = self._drain_deliveries(
            now=now,
            lease_expires_at=lease_expires_at,
            retry_at=retry_at,
            limit=self._config.max_deliveries_per_tick - len(delivery_results),
        )
        delivery_results.extend(delivered)
        return (
            tuple(provider_results),
            tuple(delivery_results),
            provider_limit_reached,
            delivery_limit_reached,
        )

    def _drain_deliveries(
        self,
        *,
        now: str,
        lease_expires_at: str,
        retry_at: str,
        limit: int,
    ) -> tuple[tuple[DeliveryRunResult, ...], bool]:
        results: list[DeliveryRunResult] = []
        if limit <= 0:
            return (), True
        for _ in range(limit):
            result = self._delivery_worker.run_once(
                worker_id=self._config.delivery_worker_id,
                now=now,
                lease_expires_at=lease_expires_at,
                retry_at=retry_at,
            )
            if result.outcome is DeliveryOutcome.IDLE:
                return tuple(results), False
            results.append(result)
        return tuple(results), True


def _timestamp(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")
