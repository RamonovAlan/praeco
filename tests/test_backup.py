from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from praeco.backup import (
    BackupError,
    apply_retention,
    create_backup,
    decode_backup_key,
    generate_backup_key,
    restore_backup,
    verify_backup,
)


def _state(tmp_path: Path) -> tuple[Path, Path]:
    database = tmp_path / "state" / "data" / "praeco.sqlite3"
    database.parent.mkdir(parents=True)
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
    connection.execute("INSERT INTO items (body) VALUES ('private thought')")
    connection.commit()
    connection.close()
    media = tmp_path / "state" / "media"
    (media / "draft").mkdir(parents=True)
    (media / "draft" / "image.png").write_bytes(b"synthetic-image")
    return database, media


def test_backup_round_trip_is_encrypted_and_verified(tmp_path: Path) -> None:
    database, media = _state(tmp_path)
    key = decode_backup_key(generate_backup_key())
    backup = create_backup(
        database_path=database,
        media_directory=media,
        output_directory=tmp_path / "backups",
        key=key,
        created_at=datetime(2026, 10, 12, 20, 0, tzinfo=UTC),
    )

    assert b"private thought" not in backup.read_bytes()
    summary = verify_backup(backup_path=backup, key=key)
    assert summary.media_files == 1

    destination = tmp_path / "restored"
    restore_backup(backup_path=backup, key=key, destination_root=destination)
    restored = sqlite3.connect(destination / "data" / "praeco.sqlite3")
    assert restored.execute("SELECT body FROM items").fetchone() == ("private thought",)
    restored.close()
    assert (destination / "media" / "draft" / "image.png").read_bytes() == b"synthetic-image"


def test_wrong_key_fails_without_partial_restore(tmp_path: Path) -> None:
    database, media = _state(tmp_path)
    backup = create_backup(
        database_path=database,
        media_directory=media,
        output_directory=tmp_path / "backups",
        key=decode_backup_key(generate_backup_key()),
    )
    destination = tmp_path / "restored"

    with pytest.raises(BackupError):
        restore_backup(
            backup_path=backup,
            key=decode_backup_key(generate_backup_key()),
            destination_root=destination,
        )

    assert not destination.exists()


def test_retention_keeps_seven_daily_and_four_older_weekly(tmp_path: Path) -> None:
    start = datetime(2026, 10, 12, 12, tzinfo=UTC)
    for offset in range(50):
        stamp = start - timedelta(days=offset)
        path = tmp_path / f"praeco-{stamp:%Y%m%dT%H%M%SZ}.backup"
        path.write_bytes(b"x")
        path.with_suffix(".backup.sha256").write_text("x", encoding="ascii")

    removed = apply_retention(tmp_path)
    remaining = list(tmp_path.glob("*.backup"))

    assert len(remaining) == 11
    assert len(removed) == 39
    assert len(list(tmp_path.glob("*.sha256"))) == 11
