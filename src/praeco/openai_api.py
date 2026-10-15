"""Dependency-free OpenAI adapters with bounded payloads and measured costs."""

import base64
import json
import secrets
import ssl
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Protocol, cast

from praeco.media import ImageValidationError, inspect_image_bytes
from praeco.providers import (
    ImageGenerationRequest,
    ImageGenerationResult,
    ProviderCallError,
    ProviderMetering,
    TextGenerationRequest,
    TextGenerationResult,
    TranscriptionRequest,
    TranscriptionResult,
)

_API_BASE_URL = "https://api.openai.com/v1"
_MAX_JSON_RESPONSE_BYTES = 30_000_000
_MAX_UPLOAD_BYTES = 20_000_000
_PRICE_VERSION = "openai-2026-10-12"
_MICRO_USD = Decimal("0.000001")


class OpenAIProviderError(ProviderCallError):
    """Sanitized provider failure that never includes request or response content."""

    def __init__(
        self,
        message: str,
        *,
        category: str = "provider_error",
        metering: ProviderMetering | None = None,
    ) -> None:
        super().__init__(message, category=category, metering=metering)


class OpenAITransport(Protocol):
    """Small HTTP boundary used by all three OpenAI provider capabilities."""

    def post_json(self, *, path: str, payload: Mapping[str, object]) -> object: ...

    def post_multipart(
        self,
        *,
        path: str,
        fields: Mapping[str, str],
        file_field: str,
        file_path: Path,
        mime_type: str,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class OpenAIProviderConfig:
    """Pinned models, prompts, and rates for the economy provider path."""

    text_model: str = "gpt-6-luna"
    transcription_model: str = "gpt-transcribe"
    image_model: str = "gpt-image-2.5-flare-2026-09-08"
    image_quality: str = "low"
    image_size: str = "1536x1024"
    text_prompt: str = field(default="", repr=False)
    image_prompt: str = field(default="", repr=False)
    style_profile: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        for field_name in (
            "text_model",
            "transcription_model",
            "image_model",
            "text_prompt",
            "image_prompt",
            "style_profile",
        ):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} must not be blank")
        if self.image_quality not in {"low", "medium", "high"}:
            raise ValueError("image_quality must be low, medium, or high")
        if self.image_size != "1536x1024":
            raise ValueError("the initial visual profile requires 1536x1024")


class UrllibOpenAITransport:
    """Official HTTPS API transport with redacted errors and bounded responses."""

    def __init__(
        self,
        *,
        api_key: str,
        timeout_seconds: int = 120,
        base_url: str = _API_BASE_URL,
    ) -> None:
        if not api_key.strip() or timeout_seconds <= 0:
            raise ValueError("OpenAI key and positive timeout are required")
        if not base_url.startswith("https://"):
            raise ValueError("OpenAI base URL must use HTTPS")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._base_url = base_url.rstrip("/")
        self._ssl_context = _ssl_context()

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(timeout_seconds={self._timeout_seconds}, "
            "api_key=<redacted>)"
        )

    def post_json(self, *, path: str, payload: Mapping[str, object]) -> object:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        return self._request(
            path=path,
            body=body,
            content_type="application/json",
        )

    def post_multipart(
        self,
        *,
        path: str,
        fields: Mapping[str, str],
        file_field: str,
        file_path: Path,
        mime_type: str,
    ) -> object:
        if not file_path.is_file() or file_path.stat().st_size > _MAX_UPLOAD_BYTES:
            raise OpenAIProviderError("OpenAI upload is missing or exceeds local limit")
        boundary = f"praeco-{secrets.token_hex(16)}"
        body = _multipart_body(
            boundary=boundary,
            fields=fields,
            file_field=file_field,
            file_path=file_path,
            mime_type=mime_type,
        )
        return self._request(
            path=path,
            body=body,
            content_type=f"multipart/form-data; boundary={boundary}",
        )

    def _request(self, *, path: str, body: bytes, content_type: str) -> object:
        if not path.startswith("/") or ".." in path:
            raise ValueError("OpenAI API path is invalid")
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": content_type,
                "User-Agent": "Praeco/0.1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self._timeout_seconds,
                context=self._ssl_context,
            ) as response:
                raw = response.read(_MAX_JSON_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            raise OpenAIProviderError(
                f"OpenAI API request failed with HTTP {error.code}"
            ) from None
        except (urllib.error.URLError, TimeoutError):
            raise OpenAIProviderError("OpenAI API connection failed") from None
        if len(raw) > _MAX_JSON_RESPONSE_BYTES:
            raise OpenAIProviderError("OpenAI API response exceeds local limit")
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise OpenAIProviderError("OpenAI API returned invalid JSON") from None


class OpenAIProvider:
    """One economy-mode adapter implementing text, transcription, and images."""

    def __init__(self, *, transport: OpenAITransport, config: OpenAIProviderConfig) -> None:
        self._transport = transport
        self._config = config

    def generate_text(self, request: TextGenerationRequest) -> TextGenerationResult:
        payload = {
            "model": self._config.text_model,
            "store": False,
            "instructions": (
                f"{self._config.text_prompt}\n\n"
                "The active owner style profile follows:\n"
                f"{self._config.style_profile}"
            ),
            "input": _text_input(request),
            "max_output_tokens": 3_200,
            "reasoning": {"effort": "low"},
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "praeco_linkedin_draft",
                    "strict": True,
                    "schema": _text_schema(),
                }
            },
        }
        response = _object(
            self._transport.post_json(path="/responses", payload=payload),
            "text response",
        )
        usage = _usage(response)
        input_tokens = _non_negative_int(usage, "input_tokens")
        output_tokens = _non_negative_int(usage, "output_tokens")
        metering = ProviderMetering(
            provider_model=self._config.text_model,
            price_version=_PRICE_VERSION,
            actual_cost_usd=_round_usd(
                Decimal(input_tokens) * Decimal("0.10") / 1_000_000
                + Decimal(output_tokens) * Decimal("0.50") / 1_000_000
            ),
            input_units=input_tokens,
            output_units=output_tokens,
        )
        if response.get("status") != "completed":
            details = response.get("incomplete_details")
            reason = details.get("reason") if isinstance(details, dict) else None
            category = "output_limit" if reason == "max_output_tokens" else "incomplete"
            raise OpenAIProviderError(
                "OpenAI text response was incomplete",
                category=category,
                metering=metering,
            )
        try:
            structured = _object(
                _json_text(_response_output_text(response)),
                "structured text output",
            )
            body = _required_string(structured, "body")
            language = _required_string(structured, "language")
            image_brief = _required_string(structured, "image_brief")
            missing_facts = _string_tuple(structured, "missing_facts")
            sensitive_items = _string_tuple(structured, "sensitive_items")
        except OpenAIProviderError as error:
            raise OpenAIProviderError(
                str(error),
                category=error.category,
                metering=metering,
            ) from None
        return TextGenerationResult(
            body=body,
            language=language,
            image_brief=image_brief,
            missing_facts=missing_facts,
            sensitive_items=sensitive_items,
            metering=metering,
        )

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        fields = {
            "model": self._config.transcription_model,
            "response_format": "json",
            "prompt": "Russian professional speech about IT, hiring, HR, and management.",
        }
        response = _object(
            self._transport.post_multipart(
                path="/audio/transcriptions",
                fields=fields,
                file_field="file",
                file_path=request.audio_path,
                mime_type=request.mime_type,
            ),
            "transcription response",
        )
        transcript = _required_string(response, "text")
        duration = request.duration_seconds
        return TranscriptionResult(
            transcript=transcript,
            language=request.language_hint or "ru",
            metering=ProviderMetering(
                provider_model=self._config.transcription_model,
                price_version=_PRICE_VERSION,
                actual_cost_usd=_round_usd(
                    duration * Decimal("0.0045") / Decimal("60")
                ),
                duration_seconds=duration,
            ),
        )

    def generate_image(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        prompt = (
            f"{self._config.image_prompt}\n\n"
            f"Approved private brief:\n{request.brief}\n\n"
            f"Current generation request:\n{request.prompt}"
        )
        common_fields = {
            "model": self._config.image_model,
            "prompt": prompt,
            "quality": self._config.image_quality,
            "size": self._config.image_size,
            "output_format": "png",
            "background": "opaque",
        }
        if request.source_image_path is None:
            response_value = self._transport.post_json(
                path="/images/generations",
                payload={**common_fields, "n": 1},
            )
        else:
            response_value = self._transport.post_multipart(
                path="/images/edits",
                fields=common_fields,
                file_field="image",
                file_path=request.source_image_path,
                mime_type=_mime_type(request.source_image_path),
            )
        response = _object(response_value, "image response")
        data = response.get("data")
        if not isinstance(data, list) or len(data) != 1:
            raise OpenAIProviderError("OpenAI image response has unexpected output count")
        image_item = _object(data[0], "image output")
        encoded = _required_string(image_item, "b64_json")
        try:
            content = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise OpenAIProviderError("OpenAI image output is invalid base64") from None
        try:
            metadata = inspect_image_bytes(content, declared_mime_type="image/png")
        except ImageValidationError:
            raise OpenAIProviderError("OpenAI image output failed local validation") from None
        usage = _usage(response)
        input_tokens = _non_negative_int(usage, "input_tokens")
        output_tokens = _non_negative_int(usage, "output_tokens")
        details = usage.get("input_tokens_details", {})
        detail_object = details if isinstance(details, dict) else {}
        text_tokens = _optional_non_negative_int(detail_object, "text_tokens") or 0
        image_tokens = _optional_non_negative_int(detail_object, "image_tokens") or 0
        unclassified_tokens = max(0, input_tokens - text_tokens - image_tokens)
        cost = (
            Decimal(text_tokens + unclassified_tokens) * Decimal("5.00")
            + Decimal(image_tokens) * Decimal("8.00")
            + Decimal(output_tokens) * Decimal("30.00")
        ) / 1_000_000
        return ImageGenerationResult(
            content=content,
            mime_type=metadata.mime_type,
            width=metadata.width,
            height=metadata.height,
            metering=ProviderMetering(
                provider_model=self._config.image_model,
                price_version=_PRICE_VERSION,
                actual_cost_usd=_round_usd(cost),
                input_units=input_tokens,
                output_units=output_tokens,
                image_quality=self._config.image_quality,
            ),
        )


def _text_input(request: TextGenerationRequest) -> str:
    parts = [
        f"Target language: {request.language}",
        f"Owner source thought:\n{request.source_text}",
    ]
    if request.current_text is not None:
        parts.append(f"Current approved-for-revision draft:\n{request.current_text}")
    if request.revision_comment is not None:
        parts.append(f"Owner revision request:\n{request.revision_comment}")
    if request.style_rules:
        parts.append("Additional active rules:\n" + "\n".join(request.style_rules))
    return "\n\n".join(parts)


def _text_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "body": {"type": "string"},
            "language": {"type": "string"},
            "image_brief": {"type": "string"},
            "missing_facts": {"type": "array", "items": {"type": "string"}},
            "sensitive_items": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "body",
            "language",
            "image_brief",
            "missing_facts",
            "sensitive_items",
        ],
        "additionalProperties": False,
    }


def _response_output_text(response: Mapping[str, object]) -> str:
    output = response.get("output")
    if not isinstance(output, list):
        raise OpenAIProviderError("OpenAI text response has no output list")
    texts: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "refusal":
                raise OpenAIProviderError("OpenAI declined the text request")
            if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                texts.append(cast(str, part["text"]))
    if not texts:
        raise OpenAIProviderError("OpenAI text response has no output text")
    return "".join(texts)


def _multipart_body(
    *,
    boundary: str,
    fields: Mapping[str, str],
    file_field: str,
    file_path: Path,
    mime_type: str,
) -> bytes:
    chunks: list[bytes] = []
    marker = boundary.encode("ascii")
    for name, value in fields.items():
        chunks.extend(
            (
                b"--" + marker + b"\r\n",
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode("utf-8"),
                b"\r\n",
            )
        )
    safe_name = file_path.name.replace('"', "")
    chunks.extend(
        (
            b"--" + marker + b"\r\n",
            (
                f'Content-Disposition: form-data; name="{file_field}"; '
                f'filename="{safe_name}"\r\n'
            ).encode(),
            f"Content-Type: {mime_type}\r\n\r\n".encode(),
            file_path.read_bytes(),
            b"\r\n--" + marker + b"--\r\n",
        )
    )
    return b"".join(chunks)


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise OpenAIProviderError(f"OpenAI {label} must be an object")
    return cast(dict[str, object], value)


def _json_text(value: str) -> object:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        raise OpenAIProviderError("OpenAI structured text is invalid JSON") from None


def _required_string(value: Mapping[str, object], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result.strip():
        raise OpenAIProviderError(f"OpenAI response field is invalid: {key}")
    return result


def _string_tuple(value: Mapping[str, object], key: str) -> tuple[str, ...]:
    result = value.get(key)
    if not isinstance(result, list) or any(
        not isinstance(item, str) or not item.strip() for item in result
    ):
        raise OpenAIProviderError(f"OpenAI response field is invalid: {key}")
    return tuple(cast(list[str], result))


def _usage(response: Mapping[str, object]) -> dict[str, object]:
    return _object(response.get("usage"), "usage")


def _non_negative_int(value: Mapping[str, object], key: str) -> int:
    result = value.get(key)
    if not isinstance(result, int) or isinstance(result, bool) or result < 0:
        raise OpenAIProviderError(f"OpenAI usage field is invalid: {key}")
    return result


def _optional_non_negative_int(value: Mapping[str, object], key: str) -> int | None:
    result = value.get(key)
    if result is None:
        return None
    if not isinstance(result, int) or isinstance(result, bool) or result < 0:
        raise OpenAIProviderError(f"OpenAI usage field is invalid: {key}")
    return result


def _round_usd(value: Decimal) -> Decimal:
    return value.quantize(_MICRO_USD, rounding=ROUND_CEILING)


def _mime_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".png":
        return "image/png"
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    raise OpenAIProviderError("source image format is unsupported")


def _ssl_context() -> ssl.SSLContext:
    default_paths = ssl.get_default_verify_paths()
    system_cafile = Path("/etc/ssl/cert.pem")
    if default_paths.cafile is None and system_cafile.is_file():
        return ssl.create_default_context(cafile=str(system_cafile))
    return ssl.create_default_context()
