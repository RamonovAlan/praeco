"""Narrow AI provider contracts and deterministic no-network test adapters."""

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from praeco.jobs import UsageMetadata


class ProviderCallError(RuntimeError):
    """Sanitized post-boundary failure with enough metadata for safe accounting."""

    def __init__(
        self,
        message: str,
        *,
        category: str,
        metering: "ProviderMetering | None" = None,
    ) -> None:
        if not category.strip():
            raise ValueError("provider error category must not be blank")
        super().__init__(message)
        self.category = category
        self.metering = metering


@dataclass(frozen=True, slots=True)
class ProviderMetering:
    """Measured provider usage and cost for one completed call."""

    provider_model: str
    price_version: str
    actual_cost_usd: Decimal
    input_units: int | None = None
    output_units: int | None = None
    duration_seconds: Decimal | None = None
    image_quality: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("provider_model", "price_version"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} must not be blank")
        if self.actual_cost_usd < 0:
            raise ValueError("actual_cost_usd must not be negative")
        for field_name in ("input_units", "output_units"):
            value = getattr(self, field_name)
            if value is not None and value < 0:
                raise ValueError(f"{field_name} must not be negative")
        if self.duration_seconds is not None and self.duration_seconds < 0:
            raise ValueError("duration_seconds must not be negative")
        if self.image_quality is not None and not self.image_quality.strip():
            raise ValueError("image_quality must not be blank")

    def usage_metadata(self, *, stage: str, now: str) -> UsageMetadata:
        """Convert provider metering into the durable job-usage schema."""

        return UsageMetadata(
            stage=stage,
            provider_model=self.provider_model,
            price_version=self.price_version,
            now=now,
            input_units=self.input_units,
            output_units=self.output_units,
            duration_seconds=self.duration_seconds,
            image_quality=self.image_quality,
        )


@dataclass(frozen=True, slots=True)
class TextGenerationRequest:
    """Compact text context for one draft or revision call."""

    source_text: str = field(repr=False)
    language: str
    current_text: str | None = field(default=None, repr=False)
    revision_comment: str | None = field(default=None, repr=False)
    style_rules: tuple[str, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        _require_text(self.source_text, "source_text")
        _require_text(self.language, "language")
        _require_optional_text(self.current_text, "current_text")
        _require_optional_text(self.revision_comment, "revision_comment")
        if any(not rule.strip() for rule in self.style_rules):
            raise ValueError("style_rules must not contain blank rules")


@dataclass(frozen=True, slots=True)
class TextGenerationResult:
    """Validated structured output expected from a text provider."""

    body: str = field(repr=False)
    language: str
    image_brief: str = field(repr=False)
    metering: ProviderMetering
    missing_facts: tuple[str, ...] = field(default=(), repr=False)
    sensitive_items: tuple[str, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        _require_text(self.body, "body")
        _require_text(self.language, "language")
        _require_text(self.image_brief, "image_brief")
        if any(not item.strip() for item in self.missing_facts):
            raise ValueError("missing_facts must not contain blank items")
        if any(not item.strip() for item in self.sensitive_items):
            raise ValueError("sensitive_items must not contain blank items")


@dataclass(frozen=True, slots=True)
class TranscriptionRequest:
    """Local voice-file reference for one transcription call."""

    audio_path: Path = field(repr=False)
    mime_type: str
    duration_seconds: Decimal
    language_hint: str | None = None

    def __post_init__(self) -> None:
        if not self.audio_path.name:
            raise ValueError("audio_path must identify a file")
        _require_text(self.mime_type, "mime_type")
        if self.duration_seconds < 0:
            raise ValueError("duration_seconds must not be negative")
        _require_optional_text(self.language_hint, "language_hint")


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    """One transcript plus measured audio usage."""

    transcript: str = field(repr=False)
    language: str
    metering: ProviderMetering

    def __post_init__(self) -> None:
        _require_text(self.transcript, "transcript")
        _require_text(self.language, "language")


@dataclass(frozen=True, slots=True)
class ImageGenerationRequest:
    """One approved brief and optional source image for generation or editing."""

    brief: str = field(repr=False)
    approved_text: str = field(repr=False)
    prompt: str = field(repr=False)
    width: int
    height: int
    source_image_path: Path | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _require_text(self.brief, "brief")
        _require_text(self.approved_text, "approved_text")
        _require_text(self.prompt, "prompt")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("image dimensions must be positive")


@dataclass(frozen=True, slots=True)
class ImageGenerationResult:
    """Original image bytes and technical metadata from one provider call."""

    content: bytes = field(repr=False)
    mime_type: str
    width: int
    height: int
    metering: ProviderMetering

    def __post_init__(self) -> None:
        if not self.content:
            raise ValueError("content must not be empty")
        _require_text(self.mime_type, "mime_type")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("image dimensions must be positive")


class TextProvider(Protocol):
    """Generate exactly one structured text result per request."""

    def generate_text(self, request: TextGenerationRequest) -> TextGenerationResult: ...


class TranscriptionProvider(Protocol):
    """Transcribe exactly one local voice file per request."""

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult: ...


class ImageProvider(Protocol):
    """Generate or edit exactly one original image per request."""

    def generate_image(self, request: ImageGenerationRequest) -> ImageGenerationResult: ...


class MockTextProvider:
    """Return a configured synthetic result without network access."""

    def __init__(self, result: TextGenerationResult) -> None:
        self._result = result
        self.calls = 0
        self.requests: list[TextGenerationRequest] = []

    def generate_text(self, request: TextGenerationRequest) -> TextGenerationResult:
        self.calls += 1
        self.requests.append(request)
        return self._result


class MockTranscriptionProvider:
    """Return a configured synthetic transcript without reading the audio file."""

    def __init__(self, result: TranscriptionResult) -> None:
        self._result = result
        self.calls = 0

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        self.calls += 1
        return self._result


class MockImageProvider:
    """Return configured synthetic image bytes without network access."""

    def __init__(self, result: ImageGenerationResult) -> None:
        self._result = result
        self.calls = 0
        self.requests: list[ImageGenerationRequest] = []

    def generate_image(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        self.calls += 1
        self.requests.append(request)
        return self._result


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")


def _require_optional_text(value: str | None, field_name: str) -> None:
    if value is not None and not value.strip():
        raise ValueError(f"{field_name} must not be blank")
