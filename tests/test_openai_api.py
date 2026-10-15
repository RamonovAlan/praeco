import base64
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path

import pytest

from praeco.openai_api import OpenAIProvider, OpenAIProviderConfig, OpenAIProviderError
from praeco.providers import (
    ImageGenerationRequest,
    TextGenerationRequest,
    TranscriptionRequest,
)


def png_bytes(*, width: int = 1536, height: int = 1024) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
        + b"\x00\x00\x00\x00"
    )


class FakeTransport:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, object]] = []

    def post_json(self, *, path: str, payload: Mapping[str, object]) -> object:
        self.calls.append(("json", path, payload))
        return self.responses.pop(0)

    def post_multipart(
        self,
        *,
        path: str,
        fields: Mapping[str, str],
        file_field: str,
        file_path: Path,
        mime_type: str,
    ) -> object:
        self.calls.append(
            (
                "multipart",
                path,
                {
                    "fields": dict(fields),
                    "file_field": file_field,
                    "file_path": file_path,
                    "mime_type": mime_type,
                },
            )
        )
        return self.responses.pop(0)


def provider(transport: FakeTransport) -> OpenAIProvider:
    return OpenAIProvider(
        transport=transport,
        config=OpenAIProviderConfig(
            text_prompt="Return a grounded Russian post.",
            image_prompt="Create an editorial visual.",
            style_profile="Never invent facts.",
        ),
    )


def test_generates_structured_text_and_measures_cost() -> None:
    response = {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": (
                            '{"body":"Черновик","language":"ru",'
                            '"image_brief":"Схема","missing_facts":[],'
                            '"sensitive_items":[]}'
                        ),
                    }
                ],
            }
        ],
        "usage": {"input_tokens": 1000, "output_tokens": 200},
    }
    transport = FakeTransport([response])

    result = provider(transport).generate_text(
        TextGenerationRequest(source_text="Наблюдение владельца", language="ru")
    )

    assert result.body == "Черновик"
    assert result.metering.actual_cost_usd == Decimal("0.000200")
    method, path, payload = transport.calls[0]
    assert (method, path) == ("json", "/responses")
    assert isinstance(payload, dict)
    assert payload["store"] is False
    assert payload["max_output_tokens"] == 3200
    assert payload["reasoning"] == {"effort": "low"}
    assert payload["text"]["format"]["strict"] is True  # type: ignore[index]


def test_transcribes_one_file_and_prices_duration(tmp_path: Path) -> None:
    audio_path = tmp_path / "voice.wav"
    audio_path.write_bytes(b"synthetic audio")
    transport = FakeTransport([{"text": "Проверка расшифровки."}])

    result = provider(transport).transcribe(
        TranscriptionRequest(
            audio_path=audio_path,
            mime_type="audio/wav",
            duration_seconds=Decimal("10"),
            language_hint="ru",
        )
    )

    assert result.transcript == "Проверка расшифровки."
    assert result.metering.actual_cost_usd == Decimal("0.000750")
    assert transport.calls[0][0:2] == ("multipart", "/audio/transcriptions")


def test_generates_valid_image_and_prices_usage() -> None:
    content = png_bytes()
    transport = FakeTransport(
        [
            {
                "data": [{"b64_json": base64.b64encode(content).decode("ascii")}],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 1000,
                    "input_tokens_details": {
                        "text_tokens": 100,
                        "image_tokens": 0,
                    },
                },
            }
        ]
    )

    result = provider(transport).generate_image(
        ImageGenerationRequest(
            brief="Спокойная рабочая сцена",
            approved_text="Утверждённый текст поста",
            prompt="Первая версия",
            width=1536,
            height=1024,
        )
    )

    assert result.content == content
    assert (result.width, result.height) == (1536, 1024)
    assert result.metering.actual_cost_usd == Decimal("0.030500")
    method, path, payload = transport.calls[0]
    assert (method, path) == ("json", "/images/generations")
    assert isinstance(payload, dict)
    assert payload["quality"] == "low"
    assert "Утверждённый текст поста" in str(payload["prompt"])


def test_rejects_refusal_without_leaking_output() -> None:
    private_refusal = "private response detail"
    transport = FakeTransport(
        [
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "refusal", "refusal": private_refusal}],
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        ]
    )

    with pytest.raises(OpenAIProviderError) as caught:
        provider(transport).generate_text(
            TextGenerationRequest(source_text="private input", language="ru")
        )

    assert private_refusal not in str(caught.value)
    assert "private input" not in str(caught.value)


def test_incomplete_text_response_preserves_safe_usage_for_accounting() -> None:
    transport = FakeTransport(
        [
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [],
                "usage": {"input_tokens": 900, "output_tokens": 3200},
            }
        ]
    )

    with pytest.raises(OpenAIProviderError) as caught:
        provider(transport).generate_text(
            TextGenerationRequest(source_text="Long synthetic input", language="ru")
        )

    assert caught.value.category == "output_limit"
    assert caught.value.metering is not None
    assert caught.value.metering.output_units == 3200
    assert caught.value.metering.actual_cost_usd == Decimal("0.001690")
