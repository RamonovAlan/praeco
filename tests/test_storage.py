import sqlite3
from pathlib import Path

import pytest

from praeco.storage import (
    MigrationError,
    apply_migrations,
    connect_database,
    immediate_transaction,
)

MIGRATIONS = Path(__file__).parents[1] / "migrations"


def test_initial_migration_creates_required_tables(tmp_path: Path) -> None:
    database_path = tmp_path / "praeco.sqlite3"
    connection = connect_database(database_path)
    try:
        assert apply_migrations(connection, MIGRATIONS) == (
            1,
            2,
            3,
            4,
            5,
            6,
            7,
            8,
            9,
            10,
            11,
            12,
            13,
            14,
        )
        assert apply_migrations(connection, MIGRATIONS) == ()
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        connection.close()

    assert {
        "drafts",
        "text_versions",
        "image_versions",
        "preference_rules",
        "jobs",
        "publications",
        "processed_updates",
        "api_usage",
        "account_connections",
        "telegram_outbox",
        "pending_text_inputs",
        "text_revision_requests",
        "pending_image_inputs",
        "image_revision_requests",
        "job_dependencies",
        "voice_revision_inputs",
        "linkedin_oauth_sessions",
        "schema_migrations",
    } <= tables


def test_connection_enables_foreign_keys_wal_and_busy_timeout(tmp_path: Path) -> None:
    connection = connect_database(tmp_path / "praeco.sqlite3")
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        connection.close()


def test_nested_transactions_use_savepoints_and_roll_back_with_the_outer_scope(
    tmp_path: Path,
) -> None:
    connection = connect_database(tmp_path / "praeco.sqlite3")
    connection.execute("CREATE TABLE example (id INTEGER PRIMARY KEY)")
    connection.commit()

    with (
        pytest.raises(RuntimeError, match="outer failure"),
        immediate_transaction(connection),
    ):
        connection.execute("INSERT INTO example VALUES (1)")
        with immediate_transaction(connection):
            connection.execute("INSERT INTO example VALUES (2)")
        raise RuntimeError("outer failure")

    assert connection.execute("SELECT COUNT(*) FROM example").fetchone()[0] == 0
    connection.close()


def test_connection_creates_a_missing_private_parent_directory(tmp_path: Path) -> None:
    database_path = tmp_path / "runtime" / "praeco.sqlite3"

    connection = connect_database(database_path)
    connection.close()

    assert database_path.is_file()
    assert database_path.parent.stat().st_mode & 0o777 == 0o700


def test_schema_rejects_an_image_for_a_missing_text_version(tmp_path: Path) -> None:
    connection = connect_database(tmp_path / "praeco.sqlite3")
    apply_migrations(connection, MIGRATIONS)
    connection.execute(
        """
        INSERT INTO drafts (
            id, owner_id, source_kind, source_text, language, state, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("draft-1", 101, "text", "thought", "ru", "RECEIVED", "now", "now"),
    )

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO image_versions (
                draft_id, version, source_text_version, brief, local_path, sha256,
                mime_type, width, height, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "draft-1",
                1,
                1,
                "brief",
                "media/image.png",
                "a" * 64,
                "image/png",
                1200,
                627,
                "now",
            ),
        )
    connection.close()


def test_applied_migration_checksum_change_is_rejected(tmp_path: Path) -> None:
    migration_directory = tmp_path / "migrations"
    migration_directory.mkdir()
    migration_path = migration_directory / "0001_test.sql"
    migration_path.write_text("CREATE TABLE example (id INTEGER PRIMARY KEY);\n")
    connection = connect_database(tmp_path / "praeco.sqlite3")
    apply_migrations(connection, migration_directory)
    migration_path.write_text(
        "CREATE TABLE example (id INTEGER PRIMARY KEY, changed TEXT);\n"
    )

    with pytest.raises(MigrationError, match="has changed"):
        apply_migrations(connection, migration_directory)
    connection.close()
