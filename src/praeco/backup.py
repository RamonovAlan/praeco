"""Encrypted, manifest-verified backups for the SQLite and media state."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_MAGIC = b"PRAECOBK1"
_NONCE_BYTES = 12
_TAG_BYTES = 16
_CHUNK_BYTES = 1024 * 1024
_BACKUP_PATTERN = re.compile(r"^praeco-(?P<stamp>\d{8}T\d{6}Z)\.backup$")


class BackupError(RuntimeError):
    """Raised when a backup cannot be safely created, verified, or restored."""


@dataclass(frozen=True, slots=True)
class BackupSummary:
    """Non-sensitive facts proven while verifying one backup."""

    created_at: str
    media_files: int
    total_bytes: int


def generate_backup_key() -> str:
    """Return a URL-safe encoded 256-bit encryption key."""

    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


def decode_backup_key(value: str) -> bytes:
    """Decode and validate the dedicated backup key."""

    try:
        key = base64.b64decode(value.strip(), altchars=b"-_", validate=True)
    except (ValueError, TypeError) as error:
        raise BackupError("backup encryption key is invalid") from error
    if len(key) != 32:
        raise BackupError("backup encryption key must decode to 32 bytes")
    return key


def create_backup(
    *,
    database_path: Path,
    media_directory: Path,
    output_directory: Path,
    key: bytes,
    created_at: datetime | None = None,
) -> Path:
    """Create one atomic encrypted archive from a live WAL database and media."""

    _validate_key(key)
    database_path = database_path.resolve()
    media_directory = media_directory.resolve()
    output_directory = output_directory.resolve()
    if not database_path.is_file() or not media_directory.is_dir():
        raise BackupError("database or media source is missing")
    if output_directory == media_directory or output_directory.is_relative_to(
        media_directory
    ):
        raise BackupError("backup output must be outside the media directory")

    timestamp = (created_at or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    output_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    output_directory.chmod(0o700)
    final_path = output_directory / f"praeco-{timestamp:%Y%m%dT%H%M%SZ}.backup"
    if final_path.exists():
        raise BackupError("backup for this timestamp already exists")

    try:
        with tempfile.TemporaryDirectory(
            prefix=".praeco-backup-", dir=output_directory
        ) as temporary:
            temporary_root = Path(temporary)
            payload_root = temporary_root / "payload"
            payload_root.mkdir(mode=0o700)
            database_copy = payload_root / "database.sqlite3"
            media_copy = payload_root / "media"
            media_copy.mkdir(mode=0o700)

            _snapshot_state(
                database_path=database_path,
                database_copy=database_copy,
                media_directory=media_directory,
                media_copy=media_copy,
            )
            manifest = _build_manifest(payload_root=payload_root, created_at=timestamp)
            manifest_path = payload_root / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            manifest_path.chmod(0o600)

            tar_path = temporary_root / "payload.tar"
            with tarfile.open(tar_path, "w") as archive:
                for path in sorted(payload_root.rglob("*")):
                    if path.is_file():
                        archive.add(path, arcname=path.relative_to(payload_root))

            encrypted_path = temporary_root / "encrypted.part"
            _encrypt_file(source=tar_path, destination=encrypted_path, key=key)
            encrypted_path.chmod(0o600)
            os.replace(encrypted_path, final_path)
            final_path.chmod(0o600)
            _write_checksum(final_path)
    except (OSError, sqlite3.Error, tarfile.TarError, ValueError) as error:
        if isinstance(error, BackupError):
            raise
        raise BackupError("backup creation failed") from error
    return final_path


def verify_backup(*, backup_path: Path, key: bytes) -> BackupSummary:
    """Authenticate, extract, hash-check, and integrity-check one backup."""

    _validate_key(key)
    backup_path = backup_path.resolve()
    _verify_checksum(backup_path)
    with tempfile.TemporaryDirectory(prefix="praeco-verify-") as temporary:
        payload_root = _decrypt_and_extract(
            backup_path=backup_path, key=key, temporary_root=Path(temporary)
        )
        return _verify_payload(payload_root)


def restore_backup(
    *, backup_path: Path, key: bytes, destination_root: Path
) -> BackupSummary:
    """Restore a verified backup only into an absent or empty destination."""

    _validate_key(key)
    destination_root = destination_root.resolve()
    if destination_root.exists() and any(destination_root.iterdir()):
        raise BackupError("restore destination must be empty")
    destination_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination_root.chmod(0o700)

    try:
        with tempfile.TemporaryDirectory(prefix="praeco-restore-") as temporary:
            payload_root = _decrypt_and_extract(
                backup_path=backup_path.resolve(),
                key=key,
                temporary_root=Path(temporary),
            )
            summary = _verify_payload(payload_root)
            data_target = destination_root / "data"
            media_target = destination_root / "media"
            data_target.mkdir(mode=0o700)
            shutil.copy2(payload_root / "database.sqlite3", data_target / "praeco.sqlite3")
            shutil.copytree(payload_root / "media", media_target)
            for path in destination_root.rglob("*"):
                path.chmod(0o700 if path.is_dir() else 0o600)
            return summary
    except BaseException:
        if destination_root.exists():
            shutil.rmtree(destination_root)
        raise


def apply_retention(output_directory: Path) -> tuple[Path, ...]:
    """Keep seven newest daily points plus four older weekly points."""

    candidates: list[tuple[datetime, Path]] = []
    for path in output_directory.glob("praeco-*.backup"):
        match = _BACKUP_PATTERN.fullmatch(path.name)
        if match is not None and path.is_file():
            stamp = datetime.strptime(match.group("stamp"), "%Y%m%dT%H%M%SZ").replace(
                tzinfo=UTC
            )
            candidates.append((stamp, path))
    candidates.sort(reverse=True)

    keep: set[Path] = set()
    daily: set[date] = set()
    weekly: set[tuple[int, int]] = set()
    for stamp, path in candidates:
        day = stamp.date()
        if day not in daily and len(daily) < 7:
            daily.add(day)
            keep.add(path)
            continue
        iso = day.isocalendar()
        week = (iso.year, iso.week)
        if day not in daily and week not in weekly and len(weekly) < 4:
            weekly.add(week)
            keep.add(path)

    removed: list[Path] = []
    for _, path in candidates:
        if path not in keep:
            path.unlink()
            path.with_suffix(path.suffix + ".sha256").unlink(missing_ok=True)
            removed.append(path)
    return tuple(removed)


def _snapshot_state(
    *,
    database_path: Path,
    database_copy: Path,
    media_directory: Path,
    media_copy: Path,
) -> None:
    guard = sqlite3.connect(database_path, timeout=30)
    source = sqlite3.connect(database_path, timeout=30)
    destination = sqlite3.connect(database_copy)
    try:
        guard.execute("PRAGMA busy_timeout = 30000")
        guard.execute("BEGIN IMMEDIATE")
        source.backup(destination)
        destination.commit()
        for source_path in sorted(media_directory.rglob("*")):
            if source_path.is_symlink():
                raise BackupError("media directory contains a symbolic link")
            if not source_path.is_file():
                continue
            relative = source_path.relative_to(media_directory)
            target = media_copy / relative
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copy2(source_path, target)
            target.chmod(0o600)
        guard.commit()
    finally:
        if guard.in_transaction:
            guard.rollback()
        destination.close()
        source.close()
        guard.close()


def _build_manifest(*, payload_root: Path, created_at: datetime) -> dict[str, object]:
    files = []
    total_bytes = 0
    for path in sorted(payload_root.rglob("*")):
        if not path.is_file():
            continue
        size = path.stat().st_size
        total_bytes += size
        files.append(
            {
                "path": path.relative_to(payload_root).as_posix(),
                "bytes": size,
                "sha256": _sha256(path),
            }
        )
    return {
        "format": "praeco-runtime-backup",
        "version": 1,
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
        "files": files,
        "total_bytes": total_bytes,
        "excluded": ["environment secrets", "encryption keys", "provider credentials"],
    }


def _verify_payload(payload_root: Path) -> BackupSummary:
    manifest_path = payload_root / "manifest.json"
    database_path = payload_root / "database.sqlite3"
    media_root = payload_root / "media"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BackupError("backup manifest is invalid") from error
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") != "praeco-runtime-backup"
        or manifest.get("version") != 1
        or not isinstance(manifest.get("files"), list)
    ):
        raise BackupError("backup manifest format is unsupported")

    listed: set[str] = set()
    total_bytes = 0
    media_files = 0
    for item in manifest["files"]:
        if not isinstance(item, dict):
            raise BackupError("backup manifest entry is invalid")
        relative = item.get("path")
        if not isinstance(relative, str) or not _safe_relative_path(relative):
            raise BackupError("backup manifest path is unsafe")
        path = payload_root / relative
        if not path.is_file() or relative in listed:
            raise BackupError("backup payload is incomplete")
        listed.add(relative)
        size = path.stat().st_size
        if item.get("bytes") != size or item.get("sha256") != _sha256(path):
            raise BackupError("backup payload checksum mismatch")
        total_bytes += size
        media_files += int(relative.startswith("media/"))

    actual = {
        path.relative_to(payload_root).as_posix()
        for path in payload_root.rglob("*")
        if path.is_file() and path != manifest_path
    }
    if actual != listed or not database_path.is_file() or not media_root.is_dir():
        raise BackupError("backup payload does not match its manifest")
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise BackupError("restored database failed integrity check")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise BackupError("restored database failed foreign-key check")
    finally:
        connection.close()
    created_at = manifest.get("created_at")
    if not isinstance(created_at, str):
        raise BackupError("backup creation time is missing")
    return BackupSummary(
        created_at=created_at, media_files=media_files, total_bytes=total_bytes
    )


def _encrypt_file(*, source: Path, destination: Path, key: bytes) -> None:
    nonce = os.urandom(_NONCE_BYTES)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(_MAGIC)
    with source.open("rb") as incoming, destination.open("xb") as outgoing:
        outgoing.write(_MAGIC)
        outgoing.write(nonce)
        while chunk := incoming.read(_CHUNK_BYTES):
            outgoing.write(encryptor.update(chunk))
        outgoing.write(encryptor.finalize())
        outgoing.write(encryptor.tag)


def _decrypt_and_extract(
    *, backup_path: Path, key: bytes, temporary_root: Path
) -> Path:
    if not backup_path.is_file():
        raise BackupError("backup file is missing")
    size = backup_path.stat().st_size
    header_size = len(_MAGIC) + _NONCE_BYTES
    if size <= header_size + _TAG_BYTES:
        raise BackupError("backup file is truncated")
    tar_path = temporary_root / "payload.tar"
    try:
        with backup_path.open("rb") as incoming:
            if incoming.read(len(_MAGIC)) != _MAGIC:
                raise BackupError("backup file format is unsupported")
            nonce = incoming.read(_NONCE_BYTES)
            incoming.seek(-_TAG_BYTES, os.SEEK_END)
            tag = incoming.read(_TAG_BYTES)
            cipher_bytes = size - header_size - _TAG_BYTES
            incoming.seek(header_size)
            decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
            decryptor.authenticate_additional_data(_MAGIC)
            with tar_path.open("xb") as outgoing:
                remaining = cipher_bytes
                while remaining:
                    chunk = incoming.read(min(_CHUNK_BYTES, remaining))
                    if not chunk:
                        raise BackupError("backup ciphertext is truncated")
                    remaining -= len(chunk)
                    outgoing.write(decryptor.update(chunk))
                outgoing.write(decryptor.finalize())
    except BackupError:
        raise
    except BaseException as error:
        raise BackupError("backup authentication failed") from error

    payload_root = temporary_root / "payload"
    payload_root.mkdir(mode=0o700)
    try:
        with tarfile.open(tar_path, "r") as archive:
            for member in archive.getmembers():
                if not member.isfile() or not _safe_relative_path(member.name):
                    raise BackupError("backup archive contains an unsafe entry")
                target = payload_root / member.name
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise BackupError("backup archive entry is unreadable")
                with source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, length=_CHUNK_BYTES)
                target.chmod(0o600)
    except (OSError, tarfile.TarError) as error:
        raise BackupError("backup archive is invalid") from error
    return payload_root


def _safe_relative_path(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts


def _write_checksum(path: Path) -> None:
    checksum_path = path.with_suffix(path.suffix + ".sha256")
    temporary = checksum_path.with_suffix(checksum_path.suffix + ".part")
    temporary.write_text(f"{_sha256(path)}  {path.name}\n", encoding="ascii")
    temporary.chmod(0o600)
    os.replace(temporary, checksum_path)


def _verify_checksum(path: Path) -> None:
    checksum_path = path.with_suffix(path.suffix + ".sha256")
    try:
        expected, name = checksum_path.read_text(encoding="ascii").strip().split("  ", 1)
    except (OSError, ValueError) as error:
        raise BackupError("backup checksum file is missing or invalid") from error
    if name != path.name or expected != _sha256(path):
        raise BackupError("backup file checksum mismatch")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_key(key: bytes) -> None:
    if len(key) != 32:
        raise BackupError("backup encryption key must be 32 bytes")
