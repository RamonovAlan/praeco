"""Run exactly one paid OpenAI call per Praeco capability and print safe metrics."""

# ruff: noqa: RUF001 -- Russian synthetic smoke-test content is intentional.

import argparse
import json
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from pathlib import Path

from praeco.local_config import read_env_file
from praeco.openai_api import (
    OpenAIProvider,
    OpenAIProviderConfig,
    OpenAIProviderError,
    UrllibOpenAITransport,
)
from praeco.providers import (
    ImageGenerationRequest,
    ProviderMetering,
    TextGenerationRequest,
    TranscriptionRequest,
)


@dataclass(frozen=True, slots=True)
class SmokeMetric:
    stage: str
    status: str
    model: str | None = None
    input_units: int | None = None
    output_units: int | None = None
    duration_seconds: str | None = None
    cost_usd: str | None = None
    detail: str | None = None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm-paid", action="store_true")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--audio-duration", type=Decimal, required=True)
    parser.add_argument("--output-image", type=Path, required=True)
    parser.add_argument(
        "--stage",
        choices=("all", "text", "transcription", "image"),
        default="all",
    )
    arguments = parser.parse_args()
    if not arguments.confirm_paid:
        parser.error("--confirm-paid is required")
    values = read_env_file(arguments.env_file)
    api_key = values.get("PRAECO_AI_API_KEY", "").strip()
    if not api_key:
        parser.error("PRAECO_AI_API_KEY is missing")
    root = arguments.env_file.parent.resolve()
    provider = OpenAIProvider(
        transport=UrllibOpenAITransport(api_key=api_key),
        config=OpenAIProviderConfig(
            text_prompt=(root / "prompts/text.md").read_text(encoding="utf-8"),
            image_prompt=(root / "prompts/image.md").read_text(encoding="utf-8"),
            style_profile=(root / "config/style.example.yaml").read_text(
                encoding="utf-8"
            ),
        ),
    )
    metrics: list[SmokeMetric] = []
    if arguments.stage in {"all", "text"}:
        try:
            text = provider.generate_text(
                TextGenerationRequest(
                    source_text=(
                        "Иногда процесс найма в IT оптимизируют под скорость отчётности, "
                        "а не под качество решения. Это наблюдение, не подтверждённое цифрами."
                    ),
                    language="ru",
                )
            )
            metrics.append(_metric("text", text.metering))
        except OpenAIProviderError as error:
            metrics.append(
                SmokeMetric(stage="text", status="failed", detail=error.category)
            )
    if arguments.stage in {"all", "transcription"}:
        try:
            transcription = provider.transcribe(
                TranscriptionRequest(
                    audio_path=arguments.audio.resolve(),
                    mime_type="audio/wav",
                    duration_seconds=arguments.audio_duration,
                    language_hint="ru",
                )
            )
            metrics.append(_metric("transcription", transcription.metering))
        except OpenAIProviderError as error:
            metrics.append(
                SmokeMetric(
                    stage="transcription",
                    status="failed",
                    detail=error.category,
                )
            )
    if arguments.stage in {"all", "image"}:
        try:
            image = provider.generate_image(
                ImageGenerationRequest(
                    brief=(
                        "Рабочая система измеряет скорость найма секундомером, пока качество "
                        "решения остаётся за затемнённым стеклом; без людей, текста и логотипов."
                    ),
                    approved_text=(
                        "Скорость найма бессмысленна без проверки качества решения."
                    ),
                    prompt="Create the first production-safe smoke-test visual.",
                    width=1536,
                    height=1024,
                )
            )
            arguments.output_image.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            arguments.output_image.write_bytes(image.content)
            arguments.output_image.chmod(0o600)
            metric = _metric("image", image.metering)
            metrics.append(replace(metric, detail=f"{image.width}x{image.height}"))
        except OpenAIProviderError as error:
            metrics.append(
                SmokeMetric(stage="image", status="failed", detail=error.category)
            )
    print(json.dumps([asdict(item) for item in metrics], ensure_ascii=False, indent=2))
    return 0 if all(item.status == "ok" for item in metrics) else 1


def _metric(stage: str, metering: ProviderMetering) -> SmokeMetric:
    return SmokeMetric(
        stage=stage,
        status="ok",
        model=metering.provider_model,
        input_units=metering.input_units,
        output_units=metering.output_units,
        duration_seconds=(
            str(metering.duration_seconds)
            if metering.duration_seconds is not None
            else None
        ),
        cost_usd=str(metering.actual_cost_usd),
    )


if __name__ == "__main__":
    raise SystemExit(main())
