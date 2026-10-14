"""Executable, fail-closed composition root for the single-process Praeco MVP."""

# ruff: noqa: RUF001 -- Russian mock-only owner content is intentional.

import argparse
import base64
import math
import os
import signal
import sqlite3
import sys
import threading
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import FrameType
from typing import Self, cast

from praeco.budget import BudgetPolicy
from praeco.dispatcher import DispatchCostEstimates, TelegramDispatcher
from praeco.health import HealthServer, HealthState, PostHandler
from praeco.linkedin import (
    LinkedInOAuthConfig,
    LinkedInOAuthService,
    decode_credential_key,
)
from praeco.linkedin_publish import LinkedInPublisher, LinkedInPublisherConfig
from praeco.local_config import read_env_file
from praeco.openai_api import (
    OpenAIProvider,
    OpenAIProviderConfig,
    OpenAIProviderError,
    UrllibOpenAITransport,
)
from praeco.outbox import DeliveryWorker
from praeco.providers import (
    ImageGenerationResult,
    ImageProvider,
    MockImageProvider,
    MockTextProvider,
    MockTranscriptionProvider,
    ProviderMetering,
    TextGenerationResult,
    TextProvider,
    TranscriptionProvider,
    TranscriptionResult,
)
from praeco.publication import PublicationRepository, PublicationWorker
from praeco.runtime import PraecoRuntime, RuntimeConfig
from praeco.storage import apply_migrations, connect_database
from praeco.telegram_api import (
    BotApiTelegramSender,
    RussianStatusResponder,
    TelegramApiError,
    TelegramBotApiClient,
    TelegramPollingAdapter,
    TelegramTransport,
    UrllibTelegramTransport,
)
from praeco.worker import SingleWorker, recover_orphaned_text_calls

_MOCK_PRICE_VERSION = "local-mock-v1"
_BUNDLED_RESOURCES = Path(__file__).resolve().parent / "resources"
_MOCK_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class ApplicationConfigError(ValueError):
    """Raised when runtime configuration is incomplete or unsafe."""


@dataclass(frozen=True, slots=True)
class ApplicationConfig:
    """Validated non-secret runtime settings plus a redacted Telegram credential."""

    owner_id: int
    telegram_token: str = field(repr=False)
    database_path: Path
    media_directory: Path
    migrations_directory: Path
    health_host: str
    health_port: int
    retry_seconds: float
    budget_policy: BudgetPolicy
    runtime_config: RuntimeConfig
    provider_mode: str = "mock"
    publishing_enabled: bool = False
    ai_api_key: str | None = field(default=None, repr=False)
    text_prompt_path: Path = Path("prompts/text.md")
    image_prompt_path: Path = Path("prompts/image.md")
    style_profile_path: Path = Path("config/style.example.yaml")
    linkedin_oauth_config: LinkedInOAuthConfig | None = None

    def __post_init__(self) -> None:
        if self.owner_id <= 0 or not self.telegram_token.strip():
            raise ApplicationConfigError("Telegram owner ID and token are required")
        if not self.health_host.strip() or not 1 <= self.health_port <= 65_535:
            raise ApplicationConfigError("health host or port is invalid")
        if self.retry_seconds <= 0:
            raise ApplicationConfigError("retry delay must be positive")
        if self.provider_mode not in {"mock", "openai"}:
            raise ApplicationConfigError("provider mode must be mock or openai")
        if self.provider_mode == "openai" and not (self.ai_api_key or "").strip():
            raise ApplicationConfigError("OpenAI provider mode requires PRAECO_AI_API_KEY")
        if self.publishing_enabled and self.linkedin_oauth_config is None:
            raise ApplicationConfigError("publishing requires LinkedIn OAuth configuration")
        if self.media_directory == Path("/"):
            raise ApplicationConfigError("media directory must not be the filesystem root")


class SignalStopper:
    """Map SIGINT and SIGTERM to one cooperative stop event and restore handlers."""

    def __init__(self, stop_event: threading.Event) -> None:
        self._stop_event = stop_event
        self._previous: dict[int, object] = {}

    def __enter__(self) -> Self:
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            self._previous[signal_number] = signal.signal(
                signal_number,
                self.request_stop,
            )
        return self

    def __exit__(
        self,
        _exception_type: object,
        _exception: object,
        _traceback: object,
    ) -> None:
        for signal_number, handler in self._previous.items():
            signal.signal(signal_number, cast(signal.Handlers, handler))
        self._previous.clear()

    def request_stop(self, _signal_number: int, _frame: FrameType | None) -> None:
        self._stop_event.set()


@dataclass(slots=True)
class PraecoApplication:
    """Own the runtime, health listener, database, and cooperative shutdown state."""

    connection: sqlite3.Connection = field(repr=False)
    runtime: PraecoRuntime = field(repr=False)
    health_state: HealthState
    health_server: HealthServer = field(repr=False)
    owner_id: int
    retry_seconds: float
    idle_seconds: float
    stop_event: threading.Event = field(default_factory=threading.Event, repr=False)
    publication_repository: PublicationRepository | None = field(
        default=None,
        repr=False,
    )

    def run(self, *, install_signal_handlers: bool = True) -> int:
        """Run until SIGINT/SIGTERM while keeping recoverable failures observable."""

        signal_context = (
            SignalStopper(self.stop_event)
            if install_signal_handlers
            else nullcontext()
        )
        try:
            recover_orphaned_text_calls(
                connection=self.connection,
                owner_id=self.owner_id,
                now=datetime.now(UTC).replace(microsecond=0).isoformat().replace(
                    "+00:00", "Z"
                ),
            )
            if self.publication_repository is not None:
                self.publication_repository.recover_inflight_as_unknown(
                    now=datetime.now(UTC).replace(microsecond=0).isoformat().replace(
                        "+00:00", "Z"
                    )
                )
            self.health_server.start()
            self.health_state.mark_ready()
            with signal_context:
                while not self.stop_event.is_set():
                    try:
                        result = self.runtime.run_tick()
                    except TelegramApiError:
                        self.health_state.record_error("telegram_unavailable")
                        self.stop_event.wait(self.retry_seconds)
                        continue
                    except sqlite3.Error:
                        if self.connection.in_transaction:
                            self.connection.rollback()
                        self.health_state.record_error("database_unavailable")
                        self.stop_event.wait(self.retry_seconds)
                        continue
                    except OpenAIProviderError:
                        self.health_state.record_error("provider_unavailable")
                        self.stop_event.wait(self.retry_seconds)
                        continue
                    self.health_state.record_tick()
                    if not result.had_activity:
                        self.stop_event.wait(self.idle_seconds)
            return 0
        except (OSError, RuntimeError, ValueError):
            self.health_state.record_error("runtime_failure")
            return 1
        finally:
            self.health_state.mark_stopping()
            self.health_server.stop()
            self.connection.close()

    def close(self) -> None:
        """Close resources for configuration checks that never start the runtime."""

        self.health_server.stop()
        self.connection.close()


def load_application_config(env_path: Path) -> ApplicationConfig:
    """Load a local env file with process-environment overrides, without evaluation."""

    values = read_env_file(env_path) if env_path.is_file() else {}
    values.update(
        {
            key: value
            for key, value in os.environ.items()
            if key.startswith("PRAECO_")
        }
    )
    base_directory = env_path.parent.resolve()
    owner_id = _positive_int(values, "PRAECO_OWNER_TELEGRAM_USER_ID")
    token = _required(values, "PRAECO_TELEGRAM_BOT_TOKEN")
    database_path = _path_value(
        values,
        "PRAECO_DATABASE_PATH",
        default="./data/praeco.sqlite3",
        base_directory=base_directory,
    )
    media_directory = _path_value(
        values,
        "PRAECO_MEDIA_DIR",
        default="./media",
        base_directory=base_directory,
    )
    migrations_directory = _path_value(
        values,
        "PRAECO_MIGRATIONS_DIR",
        default="./migrations",
        base_directory=base_directory,
        bundled_fallback=_BUNDLED_RESOURCES / "migrations",
    )
    publishing_enabled = _boolean_value(
        values,
        "PRAECO_PUBLISHING_ENABLED",
        default=False,
    )
    text_prompt_path = _path_value(
        values,
        "PRAECO_TEXT_PROMPT_PATH",
        default="./prompts/text.md",
        base_directory=base_directory,
        bundled_fallback=_BUNDLED_RESOURCES / "prompts" / "text.md",
    )
    image_prompt_path = _path_value(
        values,
        "PRAECO_IMAGE_PROMPT_PATH",
        default="./prompts/image.md",
        base_directory=base_directory,
        bundled_fallback=_BUNDLED_RESOURCES / "prompts" / "image.md",
    )
    style_profile_path = _path_value(
        values,
        "PRAECO_STYLE_PROFILE_PATH",
        default="./config/style.example.yaml",
        base_directory=base_directory,
        bundled_fallback=_BUNDLED_RESOURCES / "config" / "style.example.yaml",
    )
    linkedin_values = {
        "client_id": values.get("PRAECO_LINKEDIN_CLIENT_ID", "").strip(),
        "client_secret": values.get("PRAECO_LINKEDIN_CLIENT_SECRET", "").strip(),
        "redirect_uri": values.get("PRAECO_LINKEDIN_REDIRECT_URI", "").strip(),
        "credential_key": values.get("PRAECO_CREDENTIAL_ENCRYPTION_KEY", "").strip(),
    }
    linkedin_oauth_config: LinkedInOAuthConfig | None = None
    if any(linkedin_values.values()):
        if not all(linkedin_values.values()):
            raise ApplicationConfigError("LinkedIn OAuth configuration is incomplete")
        try:
            linkedin_oauth_config = LinkedInOAuthConfig(
                client_id=linkedin_values["client_id"],
                client_secret=linkedin_values["client_secret"],
                redirect_uri=linkedin_values["redirect_uri"],
                credential_key=decode_credential_key(linkedin_values["credential_key"]),
            )
        except ValueError as error:
            raise ApplicationConfigError("LinkedIn OAuth configuration is invalid") from error
    return ApplicationConfig(
        owner_id=owner_id,
        telegram_token=token,
        database_path=database_path,
        media_directory=media_directory,
        migrations_directory=migrations_directory,
        health_host=values.get("PRAECO_HEALTH_HOST", "127.0.0.1").strip(),
        health_port=_integer_value(values, "PRAECO_HEALTH_PORT", default=8080),
        retry_seconds=_float_value(values, "PRAECO_RETRY_SECONDS", default=5.0),
        budget_policy=BudgetPolicy.from_mapping(values),
        runtime_config=RuntimeConfig(
            polling_timeout_seconds=_integer_value(
                values,
                "PRAECO_POLLING_TIMEOUT_SECONDS",
                default=30,
            ),
            idle_sleep_seconds=_float_value(
                values,
                "PRAECO_IDLE_SLEEP_SECONDS",
                default=1.0,
            ),
        ),
        provider_mode=values.get("PRAECO_PROVIDER_MODE", "mock").strip().lower(),
        publishing_enabled=publishing_enabled,
        ai_api_key=values.get("PRAECO_AI_API_KEY"),
        text_prompt_path=text_prompt_path,
        image_prompt_path=image_prompt_path,
        style_profile_path=style_profile_path,
        linkedin_oauth_config=linkedin_oauth_config,
    )


def build_application(
    config: ApplicationConfig,
    *,
    telegram_transport: TelegramTransport | None = None,
    health_post_handler: PostHandler | None = None,
) -> PraecoApplication:
    """Apply migrations and compose the selected runtime without making API calls."""

    config.media_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    config.media_directory.chmod(0o700)
    connection = connect_database(config.database_path)
    try:
        apply_migrations(connection, config.migrations_directory)
        schema_version = int(
            connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        )
        transport = telegram_transport or UrllibTelegramTransport(
            token=config.telegram_token,
            timeout_seconds=config.runtime_config.polling_timeout_seconds + 15,
        )
        client = TelegramBotApiClient(transport)
        linkedin_oauth = (
            LinkedInOAuthService(
                database_path=config.database_path,
                config=config.linkedin_oauth_config,
            )
            if config.linkedin_oauth_config is not None
            else None
        )
        text_provider: TextProvider
        transcription_provider: TranscriptionProvider
        image_provider: ImageProvider
        if config.provider_mode == "openai":
            provider = OpenAIProvider(
                transport=UrllibOpenAITransport(api_key=config.ai_api_key or ""),
                config=OpenAIProviderConfig(
                    text_prompt=_read_instruction(config.text_prompt_path),
                    image_prompt=_read_instruction(config.image_prompt_path),
                    style_profile=_read_instruction(config.style_profile_path),
                ),
            )
            text_provider = provider
            transcription_provider = provider
            image_provider = provider
            text_estimate = Decimal("0.02")
            transcription_estimate = Decimal("0.02")
            image_estimate = Decimal("0.20")
        else:
            text_provider = MockTextProvider(_mock_text_result())
            transcription_provider = MockTranscriptionProvider(
                _mock_transcription_result()
            )
            image_provider = MockImageProvider(_mock_image_result())
            text_estimate = Decimal("0")
            transcription_estimate = Decimal("0")
            image_estimate = Decimal("0")
        dispatcher = TelegramDispatcher(
            connection=connection,
            owner_id=config.owner_id,
            budget_policy=config.budget_policy,
            cost_estimates=DispatchCostEstimates(
                text_usd=text_estimate,
                transcription_usd=transcription_estimate,
                image_usd=image_estimate,
            ),
            media_directory=config.media_directory,
            publishing_enabled=config.publishing_enabled,
        )
        poller = TelegramPollingAdapter(
            client=client,
            dispatcher=dispatcher,
            owner_id=config.owner_id,
            media_directory=config.media_directory,
            status_responder=RussianStatusResponder(
                client,
                paid_ai_enabled=config.provider_mode == "openai",
                publishing_enabled=config.publishing_enabled,
                linkedin_oauth=linkedin_oauth,
            ),
        )
        worker = SingleWorker(
            connection=connection,
            owner_id=config.owner_id,
            media_directory=config.media_directory,
            text_provider=text_provider,
            transcription_provider=transcription_provider,
            image_provider=image_provider,
            budget_policy=config.budget_policy,
            next_text_estimate_usd=text_estimate,
        )
        runtime = PraecoRuntime(
            poller=poller,
            provider_worker=worker,
            delivery_worker=DeliveryWorker(
                connection=connection,
                media_directory=config.media_directory,
                sender=BotApiTelegramSender(client),
                publishing_enabled=config.publishing_enabled,
            ),
            publication_worker=(
                PublicationWorker(
                    connection=connection,
                    media_directory=config.media_directory,
                    oauth=linkedin_oauth,
                    publisher=LinkedInPublisher(config=LinkedInPublisherConfig()),
                )
                if config.publishing_enabled and linkedin_oauth is not None
                else None
            ),
            config=config.runtime_config,
        )
        health_state = HealthState(
            schema_version=schema_version,
            provider_mode=config.provider_mode,
            publishing_enabled=config.publishing_enabled,
        )
        return PraecoApplication(
            connection=connection,
            runtime=runtime,
            health_state=health_state,
            health_server=HealthServer(
                host=config.health_host,
                port=config.health_port,
                state=health_state,
                post_handler=health_post_handler,
                linkedin_oauth=linkedin_oauth,
            ),
            owner_id=config.owner_id,
            retry_seconds=config.retry_seconds,
            idle_seconds=config.runtime_config.idle_sleep_seconds,
            publication_repository=(
                PublicationRepository(connection)
                if config.publishing_enabled
                else None
            ),
        )
    except BaseException:
        connection.close()
        raise


def main(argv: list[str] | None = None) -> int:
    """Validate configuration or run the long-lived Telegram service."""

    parser = argparse.ArgumentParser(prog="praeco")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate configuration and migrations without contacting Telegram",
    )
    arguments = parser.parse_args(argv)
    try:
        config = load_application_config(arguments.env_file)
        application = build_application(config)
    except (ApplicationConfigError, OSError, ValueError, sqlite3.Error):
        print("Praeco configuration check failed.", file=sys.stderr)
        return 2
    if arguments.check:
        application.close()
        print(
            "Praeco configuration is valid; "
            f"providers={config.provider_mode}; "
            f"publishing={'enabled' if config.publishing_enabled else 'disabled'}."
        )
        return 0
    result = application.run()
    if result != 0:
        print("Praeco stopped after a sanitized runtime failure.", file=sys.stderr)
    return result


def _mock_text_result() -> TextGenerationResult:
    return TextGenerationResult(
        body=(
            "МОК-РЕЖИМ: технический черновик создан без обращения к AI. "
            "Реальный текстовый провайдер пока отключён."
        ),
        language="ru",
        image_brief="МОК-РЕЖИМ: нейтральная техническая заглушка изображения.",
        metering=ProviderMetering(
            provider_model="mock-text",
            price_version=_MOCK_PRICE_VERSION,
            actual_cost_usd=Decimal("0"),
        ),
    )


def _mock_transcription_result() -> TranscriptionResult:
    return TranscriptionResult(
        transcript=(
            "МОК-РЕЖИМ: содержимое голосового сообщения не расшифровывалось."
        ),
        language="ru",
        metering=ProviderMetering(
            provider_model="mock-transcription",
            price_version=_MOCK_PRICE_VERSION,
            actual_cost_usd=Decimal("0"),
        ),
    )


def _mock_image_result() -> ImageGenerationResult:
    return ImageGenerationResult(
        content=_MOCK_PNG,
        mime_type="image/png",
        width=1,
        height=1,
        metering=ProviderMetering(
            provider_model="mock-image",
            price_version=_MOCK_PRICE_VERSION,
            actual_cost_usd=Decimal("0"),
        ),
    )


def _required(values: dict[str, str], key: str) -> str:
    value = values.get(key, "").strip()
    if not value:
        raise ApplicationConfigError(f"required setting is missing: {key}")
    return value


def _positive_int(values: dict[str, str], key: str) -> int:
    value = _integer_value(values, key, default=0)
    if value <= 0:
        raise ApplicationConfigError(f"setting must be positive: {key}")
    return value


def _integer_value(values: dict[str, str], key: str, *, default: int) -> int:
    raw = values.get(key, str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        raise ApplicationConfigError(f"setting must be an integer: {key}") from None


def _float_value(values: dict[str, str], key: str, *, default: float) -> float:
    raw = values.get(key, str(default)).strip()
    try:
        value = float(raw)
    except ValueError:
        raise ApplicationConfigError(f"setting must be numeric: {key}") from None
    if not math.isfinite(value):
        raise ApplicationConfigError(f"setting must be finite: {key}")
    return value


def _boolean_value(values: dict[str, str], key: str, *, default: bool) -> bool:
    raw = values.get(key, str(default).lower()).strip().lower()
    if raw not in {"true", "false"}:
        raise ApplicationConfigError(f"setting must be true or false: {key}")
    return raw == "true"


def _path_value(
    values: dict[str, str],
    key: str,
    *,
    default: str,
    base_directory: Path,
    bundled_fallback: Path | None = None,
) -> Path:
    raw = values.get(key, default).strip()
    if not raw:
        raise ApplicationConfigError(f"path setting must not be blank: {key}")
    path = Path(raw)
    resolved = (path if path.is_absolute() else base_directory / path).resolve()
    if key not in values and not resolved.exists() and bundled_fallback is not None:
        return bundled_fallback.resolve()
    return resolved


def _read_instruction(path: Path) -> str:
    if not path.is_file():
        raise ApplicationConfigError(f"instruction file is missing: {path.name}")
    value = path.read_text(encoding="utf-8")
    if not value.strip():
        raise ApplicationConfigError(f"instruction file is blank: {path.name}")
    return value
