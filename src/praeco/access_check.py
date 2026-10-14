"""Pure helpers for redacted provider-access diagnostics."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OpenAIModelSummary:
    """Non-secret capability summary derived from the model-list response."""

    total: int
    text_candidates: int
    transcription_candidates: int
    image_candidates: int


def telegram_private_start_sender_ids(payload: object) -> tuple[int, ...]:
    """Return unique numeric senders of private /start updates without message text."""

    if not isinstance(payload, dict):
        return ()
    results = payload.get("result")
    if not isinstance(results, list):
        return ()

    sender_ids: set[int] = set()
    for update in results:
        if not isinstance(update, dict):
            continue
        message = update.get("message")
        if not isinstance(message, dict):
            continue
        chat = message.get("chat")
        sender = message.get("from")
        text = message.get("text")
        if not isinstance(chat, dict) or chat.get("type") != "private":
            continue
        if not isinstance(sender, dict) or not isinstance(sender.get("id"), int):
            continue
        if not isinstance(text, str):
            continue
        command = text.split(maxsplit=1)[0]
        if command != "/start" and not command.startswith("/start@"):
            continue
        sender_ids.add(sender["id"])
    return tuple(sorted(sender_ids))


def summarize_openai_models(payload: object) -> OpenAIModelSummary:
    """Count broad capabilities without exposing account or response metadata."""

    if not isinstance(payload, dict):
        raise ValueError("OpenAI response must be an object")
    data = payload.get("data")
    if not isinstance(data, list):
        raise ValueError("OpenAI response does not contain a model list")

    model_ids = {
        model_id
        for item in data
        if isinstance(item, dict)
        and isinstance((model_id := item.get("id")), str)
    }
    transcription = {
        model_id for model_id in model_ids if "transcribe" in model_id.lower()
    }
    images = {
        model_id
        for model_id in model_ids
        if "image" in model_id.lower() or "dall-e" in model_id.lower()
    }
    excluded = transcription | images
    text = {
        model_id
        for model_id in model_ids - excluded
        if model_id.lower().startswith(("gpt-", "o1", "o3", "o4"))
        and "audio" not in model_id.lower()
        and "realtime" not in model_id.lower()
        and "tts" not in model_id.lower()
    }
    return OpenAIModelSummary(
        total=len(model_ids),
        text_candidates=len(text),
        transcription_candidates=len(transcription),
        image_candidates=len(images),
    )
