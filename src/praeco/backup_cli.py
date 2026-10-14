"""Command-line boundary for encrypted runtime backups."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from praeco.backup import (
    BackupError,
    apply_retention,
    create_backup,
    decode_backup_key,
    restore_backup,
    verify_backup,
)


def main(argv: list[str] | None = None) -> int:
    """Create, verify, or restore a backup without printing private content."""

    parser = argparse.ArgumentParser(prog="praeco-backup")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--database", type=Path, required=True)
    create.add_argument("--media", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--backup", type=Path, required=True)
    restore = subparsers.add_parser("restore")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--destination", type=Path, required=True)
    arguments = parser.parse_args(argv)

    try:
        key = decode_backup_key(os.environ.get("PRAECO_BACKUP_ENCRYPTION_KEY", ""))
        if arguments.command == "create":
            backup_path = create_backup(
                database_path=arguments.database,
                media_directory=arguments.media,
                output_directory=arguments.output,
                key=key,
            )
            summary = verify_backup(backup_path=backup_path, key=key)
            removed = apply_retention(arguments.output)
            print(
                "Praeco backup created and verified; "
                f"file={backup_path.name}; media_files={summary.media_files}; "
                f"retention_removed={len(removed)}."
            )
        elif arguments.command == "verify":
            summary = verify_backup(backup_path=arguments.backup, key=key)
            print(
                "Praeco backup verified; "
                f"created_at={summary.created_at}; media_files={summary.media_files}."
            )
        else:
            summary = restore_backup(
                backup_path=arguments.backup,
                key=key,
                destination_root=arguments.destination,
            )
            print(
                "Praeco backup restored and verified; "
                f"created_at={summary.created_at}; media_files={summary.media_files}."
            )
    except (BackupError, OSError):
        print("Praeco backup operation failed safely.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
