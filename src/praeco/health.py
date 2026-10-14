"""Minimal public health and OAuth HTTP surface."""

# ruff: noqa: RUF001 -- Russian owner-facing OAuth result text is intentional.

import json
import threading
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast

from praeco.linkedin import LinkedInOAuthService, OAuthCallbackOutcome

_LINKEDIN_CALLBACK_PATH = "/oauth/linkedin/callback"

PostHandler = Callable[[BaseHTTPRequestHandler], bool]


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    """Public, non-sensitive process state returned by health endpoints."""

    status: str
    runtime: str
    provider_mode: str
    publishing: str
    schema_version: int
    last_tick_at: str | None
    error_category: str | None

    def as_dict(self) -> dict[str, str | int | None]:
        return {
            "service": "praeco",
            "status": self.status,
            "runtime": self.runtime,
            "provider_mode": self.provider_mode,
            "publishing": self.publishing,
            "schema_version": self.schema_version,
            "last_tick_at": self.last_tick_at,
            "error_category": self.error_category,
        }


class HealthState:
    """Thread-safe state shared by the runtime loop and HTTP handler."""

    def __init__(
        self,
        *,
        schema_version: int,
        provider_mode: str = "mock",
        publishing_enabled: bool = False,
    ) -> None:
        if schema_version <= 0:
            raise ValueError("schema_version must be positive")
        if provider_mode not in {"mock", "openai"}:
            raise ValueError("provider_mode is unsupported")
        self._schema_version = schema_version
        self._provider_mode = provider_mode
        self._publishing_enabled = publishing_enabled
        self._runtime = "starting"
        self._ready = False
        self._last_tick_at: str | None = None
        self._error_category: str | None = None
        self._lock = threading.Lock()

    def mark_ready(self) -> None:
        with self._lock:
            self._runtime = "running"
            self._ready = True
            self._error_category = None

    def record_tick(self) -> None:
        with self._lock:
            self._runtime = "running"
            self._ready = True
            self._last_tick_at = _timestamp()
            self._error_category = None

    def record_error(self, category: str) -> None:
        if not category.strip():
            raise ValueError("health error category must not be blank")
        with self._lock:
            self._runtime = "degraded"
            self._ready = False
            self._error_category = category

    def mark_stopping(self) -> None:
        with self._lock:
            self._runtime = "stopping"
            self._ready = False

    def snapshot(self) -> HealthSnapshot:
        with self._lock:
            return HealthSnapshot(
                status="ok" if self._ready else "not_ready",
                runtime=self._runtime,
                provider_mode=self._provider_mode,
                publishing="enabled" if self._publishing_enabled else "disabled",
                schema_version=self._schema_version,
                last_tick_at=self._last_tick_at,
                error_category=self._error_category,
            )

    def response_for_path(
        self,
        path: str,
    ) -> tuple[HTTPStatus, dict[str, str | int | None]]:
        """Resolve one health route without exposing workflow or owner data."""

        if path == "/health/live":
            return HTTPStatus.OK, {"service": "praeco", "status": "ok"}
        if path == "/health/ready":
            snapshot = self.snapshot()
            status = (
                HTTPStatus.OK
                if snapshot.status == "ok"
                else HTTPStatus.SERVICE_UNAVAILABLE
            )
            return status, snapshot.as_dict()
        return HTTPStatus.NOT_FOUND, {
            "service": "praeco",
            "status": "not_found",
        }


class HealthServer:
    """Serve bounded health JSON, OAuth, and an optional private POST adapter."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        state: HealthState,
        post_handler: PostHandler | None = None,
        linkedin_oauth: LinkedInOAuthService | None = None,
    ) -> None:
        if not host.strip():
            raise ValueError("health host must not be blank")
        if not 0 <= port <= 65_535:
            raise ValueError("health port must be between 0 and 65535")
        self._host = host
        self._port = port
        self._state = state
        self._post_handler = post_handler
        self._linkedin_oauth = linkedin_oauth
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        if self._server is None:
            raise RuntimeError("health server is not started")
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("health server is already started")
        self._server = ThreadingHTTPServer(
            (self._host, self._port),
            self._handler_type(),
        )
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="praeco-health",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is None:
            return
        if self._thread is not None:
            self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None
        self._server = None

    def _handler_type(self) -> type[BaseHTTPRequestHandler]:
        state = self._state
        post_handler = self._post_handler
        linkedin_oauth = self._linkedin_oauth

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                request = urllib.parse.urlsplit(self.path)
                if request.path == _LINKEDIN_CALLBACK_PATH and linkedin_oauth is not None:
                    result = linkedin_oauth.handle_callback(request.query)
                    self._send_oauth_result(
                        status=result.status,
                        outcome=result.outcome,
                    )
                    return
                status, payload = state.response_for_path(request.path)
                self._send(status=status, payload=payload)

            def do_POST(self) -> None:
                if post_handler is not None and post_handler(self):
                    return
                self._send(
                    status=HTTPStatus.NOT_FOUND,
                    payload={"service": "praeco", "status": "not_found"},
                )

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def _send(
                self,
                *,
                status: HTTPStatus,
                payload: dict[str, str | int | None],
            ) -> None:
                body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                self.send_response(status.value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _send_oauth_result(
                self,
                *,
                status: HTTPStatus,
                outcome: OAuthCallbackOutcome,
            ) -> None:
                messages = {
                    OAuthCallbackOutcome.CONNECTED: (
                        "LinkedIn подключён",
                        "Можно закрыть эту страницу и вернуться в Telegram.",
                    ),
                    OAuthCallbackOutcome.DENIED: (
                        "Подключение отменено",
                        "Доступ не был выдан. Вернитесь в Telegram и повторите при необходимости.",
                    ),
                    OAuthCallbackOutcome.INVALID: (
                        "Ссылка недействительна",
                        "Запрос устарел или уже использован. Создайте новую ссылку в Telegram.",
                    ),
                    OAuthCallbackOutcome.UPSTREAM_FAILED: (
                        "LinkedIn не подключён",
                        "Сервис временно недоступен. Создайте новую ссылку в Telegram и повторите.",
                    ),
                    OAuthCallbackOutcome.TOKEN_EXCHANGE_FAILED: (
                        "LinkedIn не принял код",
                        "Проверьте Client Secret приложения LinkedIn. "
                        "Затем создайте новую ссылку в Telegram.",
                    ),
                    OAuthCallbackOutcome.TOKEN_RESPONSE_INVALID: (
                        "Ответ LinkedIn не распознан",
                        "LinkedIn вернул неожиданный ответ. "
                        "Создайте новую ссылку в Telegram и повторите.",
                    ),
                    OAuthCallbackOutcome.SCOPE_MISMATCH: (
                        "Не хватает разрешений LinkedIn",
                        "Приложению нужны openid, profile и w_member_social. "
                        "Проверьте Products и создайте новую ссылку.",
                    ),
                    OAuthCallbackOutcome.USERINFO_FAILED: (
                        "Профиль LinkedIn не получен",
                        "Токен создан, но LinkedIn не вернул профиль. "
                        "Создайте новую ссылку и повторите.",
                    ),
                    OAuthCallbackOutcome.STORAGE_FAILED: (
                        "Подключение не сохранено",
                        "LinkedIn подтвердил доступ, но Praeco не смог безопасно "
                        "сохранить его. Повторите позже.",
                    ),
                    OAuthCallbackOutcome.ACCOUNT_MISMATCH: (
                        "Подключён другой аккаунт",
                        "Praeco не заменяет ранее связанный профиль автоматически.",
                    ),
                }
                title, message = messages[outcome]
                body = (
                    "<!doctype html><html lang=\"ru\"><meta charset=\"utf-8\">"
                    "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
                    f"<title>{title}</title><main><h1>{title}</h1><p>{message}</p></main>"
                    "</html>"
                ).encode()
                self.send_response(status.value)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'none'")
                self.end_headers()
                self.wfile.write(body)

        return cast(type[BaseHTTPRequestHandler], Handler)


def _timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
