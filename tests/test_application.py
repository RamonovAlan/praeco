import os
import sqlite3
from pathlib import Path

import pytest

import praeco.application as application_module
from praeco.application import (
    ApplicationConfigError,
    build_application,
    load_application_config,
    main,
)

MIGRATIONS = Path(__file__).parents[1] / "migrations"
SYNTHETIC_TOKEN = "123456789:synthetic-token-for-tests-only"


class NoNetworkTransport:
    def __init__(self) -> None:
        self.calls = 0

    def call_json(self, **_kwargs):
        self.calls += 1
        raise AssertionError("configuration check must not call Telegram")

    def call_multipart(self, **_kwargs):
        self.calls += 1
        raise AssertionError("configuration check must not call Telegram")

    def download(self, **_kwargs):
        self.calls += 1
        raise AssertionError("configuration check must not call Telegram")


def _clear_praeco_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in tuple(os.environ):
        if key.startswith("PRAECO_"):
            monkeypatch.delenv(key)


def _write_env(path: Path, *, extra: str = "") -> None:
    values = {
        "PRAECO_OWNER_TELEGRAM_USER_ID": "101",
        "PRAECO_TELEGRAM_BOT_TOKEN": SYNTHETIC_TOKEN,
        "PRAECO_DATABASE_PATH": "./data/praeco.sqlite3",
        "PRAECO_MEDIA_DIR": "./media",
        "PRAECO_MIGRATIONS_DIR": str(MIGRATIONS),
        "PRAECO_PROVIDER_MODE": "mock",
        "PRAECO_PUBLISHING_ENABLED": "false",
        "PRAECO_PER_POST_WARNING_USD": "0.50",
        "PRAECO_PER_POST_HARD_LIMIT_USD": "0.75",
        "PRAECO_MONTHLY_BUDGET_USD": "25",
    }
    if extra:
        key, value = extra.split("=", maxsplit=1)
        values[key] = value
    path.write_text(
        "\n".join(f"{key}={value}" for key, value in values.items()),
        encoding="utf-8",
    )


def test_load_application_config_resolves_paths_and_redacts_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_praeco_environment(monkeypatch)
    env_path = tmp_path / ".env"
    _write_env(env_path)

    config = load_application_config(env_path)

    assert config.owner_id == 101
    assert config.database_path == tmp_path / "data" / "praeco.sqlite3"
    assert config.media_directory == tmp_path / "media"
    assert config.provider_mode == "mock"
    assert config.publishing_enabled is False
    assert SYNTHETIC_TOKEN not in repr(config)


def test_load_application_config_uses_bundled_runtime_assets_when_defaults_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_praeco_environment(monkeypatch)
    bundled = tmp_path / "installed-package" / "resources"
    monkeypatch.setattr(application_module, "_BUNDLED_RESOURCES", bundled)
    env_path = tmp_path / "working-directory" / ".env"
    env_path.parent.mkdir()
    env_path.write_text(
        "\n".join(
            (
                "PRAECO_OWNER_TELEGRAM_USER_ID=101",
                f"PRAECO_TELEGRAM_BOT_TOKEN={SYNTHETIC_TOKEN}",
                "PRAECO_PROVIDER_MODE=mock",
                "PRAECO_PUBLISHING_ENABLED=false",
            )
        ),
        encoding="utf-8",
    )

    config = load_application_config(env_path)

    assert config.migrations_directory == bundled / "migrations"
    assert config.text_prompt_path == bundled / "prompts" / "text.md"
    assert config.image_prompt_path == bundled / "prompts" / "image.md"
    assert config.style_profile_path == bundled / "config" / "style.example.yaml"


@pytest.mark.parametrize(
    "unsafe_setting",
    (
        "PRAECO_PROVIDER_MODE=live",
        "PRAECO_PROVIDER_MODE=openai",
        "PRAECO_PUBLISHING_ENABLED=true",
        "PRAECO_RETRY_SECONDS=nan",
    ),
)
def test_load_application_config_rejects_unsafe_runtime_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_setting: str,
) -> None:
    _clear_praeco_environment(monkeypatch)
    env_path = tmp_path / ".env"
    _write_env(env_path, extra=unsafe_setting)

    with pytest.raises(ApplicationConfigError):
        load_application_config(env_path)


def test_check_command_applies_migrations_without_network_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _clear_praeco_environment(monkeypatch)
    env_path = tmp_path / ".env"
    _write_env(env_path)
    transport = NoNetworkTransport()
    monkeypatch.setattr(
        application_module,
        "UrllibTelegramTransport",
        lambda **_kwargs: transport,
    )

    result = main(["--env-file", str(env_path), "--check"])

    assert result == 0
    assert transport.calls == 0
    output = capsys.readouterr()
    assert "providers=mock" in output.out
    assert SYNTHETIC_TOKEN not in output.out + output.err
    with sqlite3.connect(tmp_path / "data" / "praeco.sqlite3") as connection:
        assert connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0] == 13


def test_linkedin_oauth_config_is_complete_validated_and_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_praeco_environment(monkeypatch)
    env_path = tmp_path / ".env"
    _write_env(env_path)
    with env_path.open("a", encoding="utf-8") as env_file:
        env_file.write(
            "\nPRAECO_LINKEDIN_CLIENT_ID=synthetic-client"
            "\nPRAECO_LINKEDIN_CLIENT_SECRET=synthetic-secret"
            "\nPRAECO_LINKEDIN_REDIRECT_URI="
            "https://praeco.example/oauth/linkedin/callback"
            "\nPRAECO_CREDENTIAL_ENCRYPTION_KEY="
            "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA\n"
        )

    config = load_application_config(env_path)

    assert config.linkedin_oauth_config is not None
    rendered = repr(config)
    assert "synthetic-secret" not in rendered
    assert "MDAwMDAw" not in rendered


def test_incomplete_linkedin_oauth_config_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_praeco_environment(monkeypatch)
    env_path = tmp_path / ".env"
    _write_env(env_path, extra="PRAECO_LINKEDIN_CLIENT_ID=synthetic-client")

    with pytest.raises(ApplicationConfigError, match="incomplete"):
        load_application_config(env_path)


def test_publishing_mode_requires_complete_oauth_but_builds_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_praeco_environment(monkeypatch)
    env_path = tmp_path / ".env"
    _write_env(env_path)
    env_path.write_text(
        env_path.read_text(encoding="utf-8").replace(
            "PRAECO_PUBLISHING_ENABLED=false",
            "PRAECO_PUBLISHING_ENABLED=true",
        ),
        encoding="utf-8",
    )
    with env_path.open("a", encoding="utf-8") as env_file:
        env_file.write(
            "\nPRAECO_LINKEDIN_CLIENT_ID=synthetic-client"
            "\nPRAECO_LINKEDIN_CLIENT_SECRET=synthetic-secret"
            "\nPRAECO_LINKEDIN_REDIRECT_URI="
            "https://praeco.example/oauth/linkedin/callback"
            "\nPRAECO_CREDENTIAL_ENCRYPTION_KEY="
            "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA\n"
        )
    transport = NoNetworkTransport()

    application = build_application(
        load_application_config(env_path),
        telegram_transport=transport,
    )
    try:
        assert application.health_state.snapshot().publishing == "enabled"
        assert transport.calls == 0
    finally:
        application.close()


def test_composition_root_uses_injected_transport_without_calling_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_praeco_environment(monkeypatch)
    env_path = tmp_path / ".env"
    _write_env(env_path)
    transport = NoNetworkTransport()

    application = build_application(
        load_application_config(env_path),
        telegram_transport=transport,
    )
    try:
        assert application.health_state.snapshot().provider_mode == "mock"
        assert transport.calls == 0
    finally:
        application.close()


def test_openai_mode_builds_without_provider_or_telegram_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_praeco_environment(monkeypatch)
    env_path = tmp_path / ".env"
    _write_env(env_path)
    env_path.write_text(
        env_path.read_text(encoding="utf-8").replace(
            "PRAECO_PROVIDER_MODE=mock",
            "PRAECO_PROVIDER_MODE=openai\nPRAECO_AI_API_KEY=synthetic-openai-key",
        ),
        encoding="utf-8",
    )
    (tmp_path / "prompts").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "prompts" / "text.md").write_text(
        "Synthetic text instruction",
        encoding="utf-8",
    )
    (tmp_path / "prompts" / "image.md").write_text(
        "Synthetic image instruction",
        encoding="utf-8",
    )
    (tmp_path / "config" / "style.example.yaml").write_text(
        "version: synthetic",
        encoding="utf-8",
    )
    transport = NoNetworkTransport()

    config = load_application_config(env_path)
    application = build_application(config, telegram_transport=transport)
    try:
        assert config.provider_mode == "openai"
        assert "synthetic-openai-key" not in repr(config)
        assert application.health_state.snapshot().provider_mode == "openai"
        assert transport.calls == 0
    finally:
        application.close()
