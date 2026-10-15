"""Check OpenAI authentication and required model visibility without paid calls."""

import argparse
import json
import ssl
import urllib.error
import urllib.request
from pathlib import Path

from praeco.local_config import read_env_file

REQUIRED_MODELS = (
    "gpt-6-luna",
    "gpt-transcribe",
    "gpt-image-2.5-flare-2026-09-08",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    arguments = parser.parse_args()
    api_key = read_env_file(arguments.env_file).get("PRAECO_AI_API_KEY", "").strip()
    if not api_key:
        parser.error("PRAECO_AI_API_KEY is missing")
    request = urllib.request.Request(
        "https://api.openai.com/v1/models",
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "Praeco/0.1.0",
        },
        method="GET",
    )
    try:
        default_paths = ssl.get_default_verify_paths()
        system_cafile = Path("/etc/ssl/cert.pem")
        context = (
            ssl.create_default_context(cafile=str(system_cafile))
            if default_paths.cafile is None and system_cafile.is_file()
            else ssl.create_default_context()
        )
        with urllib.request.urlopen(request, timeout=30, context=context) as response:
            payload = json.loads(response.read(5_000_000))
    except urllib.error.HTTPError as error:
        print(json.dumps({"status": "failed", "http_status": error.code}))
        return 1
    except urllib.error.URLError as error:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "category": "connection",
                    "reason_type": type(error.reason).__name__,
                }
            )
        )
        return 1
    except TimeoutError:
        print(json.dumps({"status": "failed", "category": "timeout"}))
        return 1
    except json.JSONDecodeError:
        print(json.dumps({"status": "failed", "category": "invalid_payload"}))
        return 1
    data = payload.get("data") if isinstance(payload, dict) else None
    model_ids = {
        item.get("id")
        for item in data or []
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    print(
        json.dumps(
            {
                "status": "ok",
                "required_models": {
                    model: model in model_ids for model in REQUIRED_MODELS
                },
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
