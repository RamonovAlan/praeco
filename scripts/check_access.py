"""Run read-only provider checks without printing credentials or private messages."""

import argparse
import json
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import cast

from praeco.access_check import (
    summarize_openai_models,
    telegram_private_start_sender_ids,
)
from praeco.local_config import read_env_file, replace_env_value


class AccessCheckError(RuntimeError):
    """Safe diagnostic error that contains no URL, credential, or response body."""


def request_json(
    *,
    service: str,
    url: str,
    headers: dict[str, str] | None = None,
) -> object:
    """Fetch JSON while reducing network failures to redacted status messages."""

    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(
            request,
            timeout=15,
            context=trusted_ssl_context(),
        ) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise AccessCheckError(f"{service}: HTTP {error.code}") from None
    except urllib.error.URLError:
        raise AccessCheckError(f"{service}: network error") from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise AccessCheckError(f"{service}: invalid JSON response") from None


def trusted_ssl_context() -> ssl.SSLContext:
    """Use Python defaults, with the standard macOS CA bundle as a safe fallback."""

    verify_paths = ssl.get_default_verify_paths()
    if verify_paths.cafile is not None:
        return ssl.create_default_context()
    macos_ca_bundle = Path("/etc/ssl/cert.pem")
    if macos_ca_bundle.is_file():
        return ssl.create_default_context(cafile=macos_ca_bundle)
    return ssl.create_default_context()


def require_secret(values: dict[str, str], key: str) -> str:
    """Return a configured secret without logging its value."""

    value = values.get(key, "")
    if not value:
        raise AccessCheckError(f"configuration: {key} is missing")
    return value


def run(*, env_path: Path, install_owner_id: bool) -> int:
    """Check Telegram and OpenAI access and optionally install one owner ID."""

    values = read_env_file(env_path)
    telegram_token = require_secret(values, "PRAECO_TELEGRAM_BOT_TOKEN")
    openai_key = require_secret(values, "PRAECO_AI_API_KEY")

    telegram_base_url = f"https://api.telegram.org/bot{telegram_token}"
    telegram_identity = request_json(
        service="Telegram getMe",
        url=f"{telegram_base_url}/getMe",
    )
    if not isinstance(telegram_identity, dict) or telegram_identity.get("ok") is not True:
        raise AccessCheckError("Telegram getMe: API rejected the request")
    print("telegram_auth=ok")

    telegram_updates = request_json(
        service="Telegram getUpdates",
        url=f"{telegram_base_url}/getUpdates?limit=100&timeout=0",
    )
    start_senders = telegram_private_start_sender_ids(telegram_updates)
    configured_owner = values.get("PRAECO_OWNER_TELEGRAM_USER_ID", "")
    if configured_owner:
        print("telegram_owner_allowlist=already_configured")
    elif len(start_senders) == 1 and install_owner_id:
        replace_env_value(
            env_path,
            "PRAECO_OWNER_TELEGRAM_USER_ID",
            str(start_senders[0]),
        )
        print("telegram_owner_allowlist=installed")
    elif len(start_senders) == 1:
        print("telegram_owner_allowlist=one_candidate_found")
    elif not start_senders:
        print("telegram_owner_allowlist=waiting_for_private_start")
    else:
        print("telegram_owner_allowlist=multiple_candidates_manual_selection_required")

    openai_models = request_json(
        service="OpenAI models",
        url="https://api.openai.com/v1/models",
        headers={"Authorization": f"Bearer {openai_key}"},
    )
    summary = summarize_openai_models(openai_models)
    print("openai_auth=ok")
    print(f"openai_models_total={summary.total}")
    print(f"openai_text_candidates={summary.text_candidates}")
    print(f"openai_transcription_candidates={summary.transcription_candidates}")
    print(f"openai_image_candidates={summary.image_candidates}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--install-owner-id", action="store_true")
    arguments = parser.parse_args()
    try:
        return run(
            env_path=cast(Path, arguments.env_file),
            install_owner_id=cast(bool, arguments.install_owner_id),
        )
    except (AccessCheckError, OSError, ValueError) as error:
        print(f"access_check=failed ({error})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
