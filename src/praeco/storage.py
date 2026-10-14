"""SQLite connection policy and checksum-verified schema migrations."""

import hashlib
import itertools
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

_MIGRATION_PATTERN = re.compile(r"^(?P<version>[0-9]{4})_(?P<name>[a-z0-9_]+)\.sql$")
_SAVEPOINT_IDS = itertools.count(1)


class MigrationError(RuntimeError):
    """Raised when migration history is missing, malformed, or modified."""


@dataclass(frozen=True, slots=True)
class Migration:
    """One immutable SQL migration loaded from disk."""

    version: int
    name: str
    checksum: str
    sql: str


@contextmanager
def immediate_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    """Run a short write transaction or savepoint inside an existing transaction."""

    if connection.in_transaction:
        savepoint = f"praeco_nested_{next(_SAVEPOINT_IDS)}"
        connection.execute(f"SAVEPOINT {savepoint}")
        try:
            yield
        except BaseException:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        else:
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def connect_database(path: Path) -> sqlite3.Connection:
    """Open one SQLite database with the safety settings required by Praeco."""

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def apply_migrations(connection: sqlite3.Connection, directory: Path) -> tuple[int, ...]:
    """Apply new migrations and reject edits to already-applied migrations."""

    migrations = load_migrations(directory)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.commit()
    applied = {
        int(row["version"]): (str(row["name"]), str(row["checksum"]))
        for row in connection.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        )
    }

    known_versions = {migration.version for migration in migrations}
    missing_files = set(applied) - known_versions
    if missing_files:
        versions = ", ".join(str(version) for version in sorted(missing_files))
        raise MigrationError(f"applied migration files are missing: {versions}")

    newly_applied: list[int] = []
    for migration in migrations:
        previous = applied.get(migration.version)
        if previous is not None:
            if previous != (migration.name, migration.checksum):
                raise MigrationError(
                    f"applied migration {migration.version:04d} has changed"
                )
            continue
        _apply_one(connection, migration)
        newly_applied.append(migration.version)
    return tuple(newly_applied)


def load_migrations(directory: Path) -> tuple[Migration, ...]:
    """Load sorted migrations with unique monotonically named versions."""

    migrations: list[Migration] = []
    seen_versions: set[int] = set()
    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_PATTERN.fullmatch(path.name)
        if match is None:
            raise MigrationError(f"invalid migration filename: {path.name}")
        version = int(match.group("version"))
        if version in seen_versions:
            raise MigrationError(f"duplicate migration version: {version:04d}")
        seen_versions.add(version)
        sql = path.read_text(encoding="utf-8")
        migrations.append(
            Migration(
                version=version,
                name=match.group("name"),
                checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
                sql=sql,
            )
        )
    if not migrations:
        raise MigrationError("no migration files found")
    return tuple(migrations)


def _apply_one(connection: sqlite3.Connection, migration: Migration) -> None:
    escaped_name = migration.name.replace("'", "''")
    escaped_checksum = migration.checksum.replace("'", "''")
    script = (
        "BEGIN IMMEDIATE;\n"
        f"{migration.sql}\n"
        "INSERT INTO schema_migrations (version, name, checksum) VALUES "
        f"({migration.version}, '{escaped_name}', '{escaped_checksum}');\n"
        "COMMIT;"
    )
    try:
        connection.executescript(script)
    except sqlite3.Error as error:
        if connection.in_transaction:
            connection.rollback()
        raise MigrationError(
            f"failed to apply migration {migration.version:04d}"
        ) from error
