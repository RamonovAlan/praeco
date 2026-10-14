"""Minimal local environment-file handling without evaluating file contents."""

import os
import re
import tempfile
from pathlib import Path

_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


def read_env_file(path: Path) -> dict[str, str]:
    """Parse simple KEY=VALUE assignments without shell expansion or execution."""

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"invalid environment assignment on line {line_number}")
        key, value = line.split("=", maxsplit=1)
        key = key.strip()
        value = value.strip()
        if not _KEY_PATTERN.fullmatch(key):
            raise ValueError(f"invalid environment key on line {line_number}")
        if key in values:
            raise ValueError(f"duplicate environment key: {key}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def replace_env_value(path: Path, key: str, value: str) -> None:
    """Atomically replace one existing assignment while preserving file permissions."""

    if not _KEY_PATTERN.fullmatch(key):
        raise ValueError("invalid environment key")
    if "\n" in value or "\r" in value:
        raise ValueError("environment values must fit on one line")

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    matches = [
        index
        for index, line in enumerate(lines)
        if line.split("=", maxsplit=1)[0].strip() == key and "=" in line
    ]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one assignment for {key}")

    index = matches[0]
    newline = "\n" if lines[index].endswith("\n") else ""
    lines[index] = f"{key}={value}{newline}"
    mode = path.stat().st_mode & 0o777
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary_file:
            temporary_file.writelines(lines)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        temporary_path.chmod(mode)
        temporary_path.replace(path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
