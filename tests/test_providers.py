from decimal import Decimal
from pathlib import Path

import pytest

from praeco.providers import (
    ImageGenerationRequest,
    ImageGenerationResult,
    MockImageProvider,
    MockTextProvider,
    MockTranscriptionProvider,
    ProviderMetering,
    TextGenerationRequest,
    TextGenerationResult,
    TranscriptionRequest,
    TranscriptionResult,
)


def metering(**overrides: object) -> ProviderMetering:
    values: dict[str, object] = {
        "provider_model": "mock-model",
        "price_version": "2026-10-12",
        "actual_cost_usd": Decimal("0"),
    }
    values.update(overrides)
    return ProviderMetering(**values)  # type: ignore[arg-type]


def test_mock_text_provider_returns_one_structured_result() -> None:
    result = TextGenerationResult(
        body="Синтетический черновик",
        language="ru",
        image_brief="Синтетический визуальный образ",
        missing_facts=("точная дата",),
        sensitive_items=("имя клиента",),
        metering=metering(input_units=120, output_units=80),
    )
    provider = MockTextProvider(result)
    request = TextGenerationRequest(
        source_text="Приватная исходная мысль",
        language="ru",
        style_rules=("Короткие абзацы",),
    )

    assert provider.generate_text(request) is result
    assert provider.calls == 1
    assert "Приватная исходная мысль" not in repr(request)
    assert "Синтетический черновик" not in repr(result)


def test_mock_transcription_provider_does_not_read_the_audio_file() -> None:
    result = TranscriptionResult(
        transcript="Синтетическая расшифровка",
        language="ru",
        metering=metering(duration_seconds=Decimal("12.5")),
    )
    provider = MockTranscriptionProvider(result)
    request = TranscriptionRequest(
        audio_path=Path("private/voice.ogg"),
        mime_type="audio/ogg",
        language_hint="ru",
        duration_seconds=Decimal("12.5"),
    )

    assert provider.transcribe(request) is result
    assert provider.calls == 1
    assert "voice.ogg" not in repr(request)
    assert "расшифровка" not in repr(result)


def test_mock_image_provider_returns_original_bytes_once() -> None:
    result = ImageGenerationResult(
        content=b"synthetic-image-bytes",
        mime_type="image/png",
        width=1200,
        height=1200,
        metering=metering(image_quality="medium"),
    )
    provider = MockImageProvider(result)
    request = ImageGenerationRequest(
        brief="Синтетический образ",
        prompt="Без логотипов и реальных людей",
        width=1200,
        height=1200,
    )

    assert provider.generate_image(request) is result
    assert provider.calls == 1
    assert "synthetic-image-bytes" not in repr(result)
    assert "Без логотипов" not in repr(request)


def test_metering_maps_to_durable_usage_without_cost_rounding() -> None:
    measured = metering(
        actual_cost_usd=Decimal("0.012345"),
        input_units=100,
        output_units=50,
    )

    usage = measured.usage_metadata(
        stage="text_generation",
        now="2026-10-12T14:00:00Z",
    )

    assert usage.stage == "text_generation"
    assert usage.provider_model == "mock-model"
    assert usage.input_units == 100
    assert usage.output_units == 50
    assert measured.actual_cost_usd == Decimal("0.012345")


@pytest.mark.parametrize(
    "invalid",
    [
        {"provider_model": ""},
        {"actual_cost_usd": Decimal("-0.01")},
        {"input_units": -1},
        {"duration_seconds": Decimal("-1")},
    ],
)
def test_metering_rejects_invalid_values(invalid: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        metering(**invalid)
