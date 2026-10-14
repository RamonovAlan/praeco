from pathlib import Path

import pytest

from praeco.local_config import read_env_file, replace_env_value


def test_reads_values_without_shell_expansion(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# local only\nPRAECO_TOKEN='$NOT_EXPANDED'\nPRAECO_EMPTY=\n",
        encoding="utf-8",
    )

    assert read_env_file(env_path) == {
        "PRAECO_TOKEN": "$NOT_EXPANDED",
        "PRAECO_EMPTY": "",
    }


def test_replaces_one_value_and_preserves_permissions(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("PRAECO_OWNER_TELEGRAM_USER_ID=\n", encoding="utf-8")
    env_path.chmod(0o600)

    replace_env_value(env_path, "PRAECO_OWNER_TELEGRAM_USER_ID", "101")

    assert read_env_file(env_path)["PRAECO_OWNER_TELEGRAM_USER_ID"] == "101"
    assert env_path.stat().st_mode & 0o777 == 0o600


def test_rejects_duplicate_assignments(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("PRAECO_TOKEN=one\nPRAECO_TOKEN=two\n", encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate"):
        read_env_file(env_path)
