from praeco.access_check import (
    summarize_openai_models,
    telegram_private_start_sender_ids,
)


def test_extracts_only_unique_private_start_senders() -> None:
    payload = {
        "result": [
            {
                "message": {
                    "chat": {"type": "private"},
                    "from": {"id": 101},
                    "text": "/start",
                }
            },
            {
                "message": {
                    "chat": {"type": "private"},
                    "from": {"id": 101},
                    "text": "/start payload",
                }
            },
            {
                "message": {
                    "chat": {"type": "group"},
                    "from": {"id": 202},
                    "text": "/start",
                }
            },
            {
                "message": {
                    "chat": {"type": "private"},
                    "from": {"id": 303},
                    "text": "ordinary message",
                }
            },
            {
                "message": {
                    "chat": {"type": "private"},
                    "from": {"id": 404},
                    "text": "/starter",
                }
            },
        ]
    }

    assert telegram_private_start_sender_ids(payload) == (101,)


def test_summarizes_models_without_returning_identifiers() -> None:
    summary = summarize_openai_models(
        {
            "data": [
                {"id": "gpt-example"},
                {"id": "gpt-example-transcribe"},
                {"id": "gpt-image-example"},
                {"id": "unrelated-model"},
            ]
        }
    )

    assert summary.total == 4
    assert summary.text_candidates == 1
    assert summary.transcription_candidates == 1
    assert summary.image_candidates == 1
