"""Owner-only summaries and versioned preference rules."""

import hashlib
import io
import json
import re
import sqlite3
import zipfile
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from praeco.storage import immediate_transaction

_BILLING_MONTH_PATTERN = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
_MICRODOLLARS_PER_DOLLAR = Decimal("1000000")
_MAX_RULE_LENGTH = 500
_MAX_ACTIVE_RULES = 20
_MAX_ACTIVE_RULE_CHARACTERS = 4_000


class OwnerDataError(RuntimeError):
    """Raised when an owner command would create an invalid durable state."""


@dataclass(frozen=True, slots=True)
class DraftSummary:
    """Non-content metadata for one recent owner draft."""

    draft_id: str
    state: str
    text_version: int
    image_version: int
    updated_at: str


@dataclass(frozen=True, slots=True)
class PreferenceRule:
    """One versioned text or image preference without implicit promotion."""

    rule_id: int
    scope: str
    rule: str = field(repr=False)
    version: int
    active: bool
    created_at: str


@dataclass(frozen=True, slots=True)
class CostSummary:
    """Current-month billed-or-estimated usage and outstanding reservations."""

    accounted_usd: Decimal
    reserved_usd: Decimal
    completed_calls: int
    unknown_billed_calls: int


@dataclass(frozen=True, slots=True)
class DeletionPlan:
    """Filesystem cleanup required after the database deletion commits."""

    draft_ids: tuple[str, ...]
    media_paths: tuple[str, ...] = field(repr=False)


class OwnerRepository:
    """Provide bounded owner views and explicit preference mutations."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def recent_drafts(self, *, owner_id: int, limit: int = 10) -> tuple[DraftSummary, ...]:
        if owner_id <= 0 or not 1 <= limit <= 20:
            raise ValueError("owner_id and limit are invalid")
        rows = self._connection.execute(
            """
            SELECT id, state, current_text_version, current_image_version, updated_at
            FROM drafts
            WHERE owner_id = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (owner_id, limit),
        ).fetchall()
        return tuple(
            DraftSummary(
                draft_id=str(row["id"]),
                state=str(row["state"]),
                text_version=int(row["current_text_version"]),
                image_version=int(row["current_image_version"]),
                updated_at=str(row["updated_at"]),
            )
            for row in rows
        )

    def preferences(
        self,
        *,
        scope: str | None = None,
        active_only: bool = False,
    ) -> tuple[PreferenceRule, ...]:
        if scope is not None:
            _validate_scope(scope)
        clauses: list[str] = []
        parameters: list[object] = []
        if scope is not None:
            clauses.append("scope = ?")
            parameters.append(scope)
        if active_only:
            clauses.append("active = 1")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"""
            SELECT id, scope, rule, version, active, created_at
            FROM preference_rules
            {where}
            ORDER BY scope, version, id
            """,
            parameters,
        ).fetchall()
        return tuple(_preference(row) for row in rows)

    def active_rule_texts(self, *, scope: str) -> tuple[str, ...]:
        return tuple(
            preference.rule
            for preference in self.preferences(scope=scope, active_only=True)
        )

    def add_preference(self, *, scope: str, rule: str, now: str) -> PreferenceRule:
        _validate_scope(scope)
        normalized = " ".join(rule.split())
        if not normalized or len(normalized) > _MAX_RULE_LENGTH:
            raise OwnerDataError("preference rule must contain 1-500 characters")
        if not now.strip():
            raise ValueError("now must not be blank")
        with immediate_transaction(self._connection):
            active = self.preferences(scope=scope, active_only=True)
            if any(item.rule.casefold() == normalized.casefold() for item in active):
                raise OwnerDataError("preference rule is already active")
            if len(active) >= _MAX_ACTIVE_RULES:
                raise OwnerDataError("active preference rule limit reached")
            active_characters = sum(len(item.rule) for item in active)
            if active_characters + len(normalized) > _MAX_ACTIVE_RULE_CHARACTERS:
                raise OwnerDataError("active preference rule text limit reached")
            version = int(
                self._connection.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 FROM preference_rules WHERE scope = ?",
                    (scope,),
                ).fetchone()[0]
            )
            cursor = self._connection.execute(
                """
                INSERT INTO preference_rules (scope, rule, source, version, active, created_at)
                VALUES (?, ?, 'owner', ?, 1, ?)
                """,
                (scope, normalized, version, now),
            )
            if cursor.lastrowid is None:
                raise OwnerDataError("SQLite did not return a preference rule ID")
            rule_id = int(cursor.lastrowid)
        return PreferenceRule(
            rule_id=rule_id,
            scope=scope,
            rule=normalized,
            version=version,
            active=True,
            created_at=now,
        )

    def set_preference_active(self, *, rule_id: int, active: bool) -> PreferenceRule:
        if rule_id <= 0:
            raise OwnerDataError("preference rule ID must be positive")
        with immediate_transaction(self._connection):
            row = self._connection.execute(
                """
                SELECT id, scope, rule, version, active, created_at
                FROM preference_rules
                WHERE id = ? AND source = 'owner'
                """,
                (rule_id,),
            ).fetchone()
            if row is None:
                raise OwnerDataError("owner preference rule does not exist")
            preference = _preference(row)
            if active and not preference.active:
                current = self.preferences(scope=preference.scope, active_only=True)
                if len(current) >= _MAX_ACTIVE_RULES:
                    raise OwnerDataError("active preference rule limit reached")
                active_characters = sum(len(item.rule) for item in current)
                if (
                    active_characters + len(preference.rule)
                    > _MAX_ACTIVE_RULE_CHARACTERS
                ):
                    raise OwnerDataError("active preference rule text limit reached")
            self._connection.execute(
                "UPDATE preference_rules SET active = ? WHERE id = ?",
                (int(active), rule_id),
            )
        return PreferenceRule(
            rule_id=preference.rule_id,
            scope=preference.scope,
            rule=preference.rule,
            version=preference.version,
            active=active,
            created_at=preference.created_at,
        )

    def cost_summary(self, *, billing_month: str) -> CostSummary:
        if _BILLING_MONTH_PATTERN.fullmatch(billing_month) is None:
            raise ValueError("billing_month must use YYYY-MM")
        usage = self._connection.execute(
            """
            SELECT
                COALESCE(SUM(COALESCE(actual_cost_microusd, estimated_cost_microusd)), 0),
                COUNT(*),
                COALESCE(SUM(CASE WHEN billing_status = 'UNKNOWN_BILLED' THEN 1 ELSE 0 END), 0)
            FROM api_usage
            WHERE billing_month = ?
            """,
            (billing_month,),
        ).fetchone()
        reserved = int(
            self._connection.execute(
                """
                SELECT COALESCE(SUM(estimated_cost_microusd), 0)
                FROM jobs
                WHERE billing_month = ?
                  AND state IN ('QUEUED', 'LEASED', 'RESULT_UNKNOWN')
                  AND NOT EXISTS (
                      SELECT 1 FROM api_usage WHERE api_usage.job_id = jobs.id
                  )
                """,
                (billing_month,),
            ).fetchone()[0]
        )
        return CostSummary(
            accounted_usd=_microusd_to_usd(int(usage[0])),
            reserved_usd=_microusd_to_usd(reserved),
            completed_calls=int(usage[1]),
            unknown_billed_calls=int(usage[2]),
        )

    def delete_all_owner_data(self, *, owner_id: int) -> DeletionPlan:
        """Delete one owner's content while preserving the running bot database."""

        if owner_id <= 0:
            raise ValueError("owner_id must be positive")
        with immediate_transaction(self._connection):
            draft_rows = self._connection.execute(
                "SELECT id FROM drafts WHERE owner_id = ? ORDER BY id",
                (owner_id,),
            ).fetchall()
            draft_ids = tuple(str(row["id"]) for row in draft_rows)
            media_rows = self._connection.execute(
                """
                SELECT image_versions.local_path
                FROM image_versions
                JOIN drafts ON drafts.id = image_versions.draft_id
                WHERE drafts.owner_id = ?
                ORDER BY image_versions.draft_id, image_versions.version
                """,
                (owner_id,),
            ).fetchall()
            media_paths = tuple(str(row["local_path"]) for row in media_rows)
            self._connection.execute(
                """
                DELETE FROM job_dependencies
                WHERE job_id IN (
                    SELECT jobs.id FROM jobs
                    JOIN drafts ON drafts.id = jobs.draft_id
                    WHERE drafts.owner_id = ?
                ) OR prerequisite_job_id IN (
                    SELECT jobs.id FROM jobs
                    JOIN drafts ON drafts.id = jobs.draft_id
                    WHERE drafts.owner_id = ?
                )
                """,
                (owner_id, owner_id),
            )
            self._connection.execute(
                """
                DELETE FROM api_usage
                WHERE draft_id IN (SELECT id FROM drafts WHERE owner_id = ?)
                """,
                (owner_id,),
            )
            self._connection.execute(
                """
                DELETE FROM publications
                WHERE draft_id IN (SELECT id FROM drafts WHERE owner_id = ?)
                """,
                (owner_id,),
            )
            self._connection.execute("DELETE FROM drafts WHERE owner_id = ?", (owner_id,))
            self._connection.execute("DELETE FROM preference_rules")
            self._connection.execute("DELETE FROM account_connections")
            self._connection.execute("DELETE FROM linkedin_oauth_sessions")
        return DeletionPlan(draft_ids=draft_ids, media_paths=media_paths)


class OwnerExporter:
    """Build a bounded owner-only archive without credentials or internal tokens."""

    def __init__(self, *, connection: sqlite3.Connection, media_directory: Path) -> None:
        self._connection = connection
        self._media_directory = media_directory

    def build_zip(self, *, owner_id: int, max_bytes: int) -> bytes:
        if owner_id <= 0 or max_bytes <= 0:
            raise ValueError("owner_id and max_bytes must be positive")
        draft_ids = tuple(
            str(row["id"])
            for row in self._connection.execute(
                "SELECT id FROM drafts WHERE owner_id = ? ORDER BY created_at, id",
                (owner_id,),
            ).fetchall()
        )
        payload = {
            "format": "praeco-owner-export-v1",
            "drafts": self._rows(
                """
                SELECT id, source_kind, source_text, language, state,
                       current_text_version, current_image_version, created_at, updated_at
                FROM drafts WHERE owner_id = ? ORDER BY created_at, id
                """,
                (owner_id,),
            ),
            "text_versions": self._rows_for_owner("text_versions", owner_id),
            "image_versions": self._rows_for_owner(
                "image_versions",
                owner_id,
                excluded={"local_path"},
            ),
            "preference_rules": self._rows(
                """
                SELECT id, scope, rule, source, version, active, created_at
                FROM preference_rules ORDER BY scope, version, id
                """
            ),
            "publications": self._rows_for_owner("publications", owner_id),
            "api_usage": self._rows_for_owner("api_usage", owner_id),
            "account_connections": self._rows(
                """
                SELECT platform, account_id, scopes_json, expires_at, created_at, updated_at
                FROM account_connections ORDER BY platform, account_id
                """
            ),
        }
        json_content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        if len(json_content) > max_bytes:
            raise OwnerDataError("owner export exceeds the Telegram upload limit")
        output = io.BytesIO()
        with zipfile.ZipFile(
            output,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as archive:
            archive.writestr(
                "praeco-data.json",
                json_content,
            )
            self._add_images(
                archive=archive,
                owner_id=owner_id,
                draft_ids=draft_ids,
                max_uncompressed_bytes=max_bytes - len(json_content),
            )
        content = output.getvalue()
        if len(content) > max_bytes:
            raise OwnerDataError("owner export exceeds the Telegram upload limit")
        return content

    def _rows_for_owner(
        self,
        table: str,
        owner_id: int,
        *,
        excluded: set[str] | None = None,
    ) -> list[dict[str, object]]:
        if table not in {"text_versions", "image_versions", "publications", "api_usage"}:
            raise ValueError("unsupported owner export table")
        rows = self._connection.execute(
            f"""
            SELECT {table}.* FROM {table}
            JOIN drafts ON drafts.id = {table}.draft_id
            WHERE drafts.owner_id = ?
            ORDER BY {table}.draft_id, {table}.id
            """
            if table in {"publications", "api_usage"}
            else f"""
            SELECT {table}.* FROM {table}
            JOIN drafts ON drafts.id = {table}.draft_id
            WHERE drafts.owner_id = ?
            ORDER BY {table}.draft_id, {table}.version
            """,
            (owner_id,),
        ).fetchall()
        return [_row_dict(row, excluded=excluded) for row in rows]

    def _rows(
        self,
        query: str,
        parameters: tuple[object, ...] = (),
    ) -> list[dict[str, object]]:
        return [
            _row_dict(row)
            for row in self._connection.execute(query, parameters).fetchall()
        ]

    def _add_images(
        self,
        *,
        archive: zipfile.ZipFile,
        owner_id: int,
        draft_ids: tuple[str, ...],
        max_uncompressed_bytes: int,
    ) -> None:
        if not draft_ids:
            return
        rows = self._connection.execute(
            """
            SELECT image_versions.draft_id, image_versions.version,
                   image_versions.local_path, image_versions.sha256
            FROM image_versions
            JOIN drafts ON drafts.id = image_versions.draft_id
            WHERE drafts.owner_id = ?
            ORDER BY image_versions.draft_id, image_versions.version
            """,
            (owner_id,),
        ).fetchall()
        media_root = self._media_directory.resolve()
        total_bytes = 0
        for row in rows:
            source = (media_root / str(row["local_path"])).resolve()
            if not source.is_relative_to(media_root) or not source.is_file():
                raise OwnerDataError("owner export image is missing")
            total_bytes += source.stat().st_size
            if total_bytes > max_uncompressed_bytes:
                raise OwnerDataError("owner export exceeds the Telegram upload limit")
            content = source.read_bytes()
            if hashlib.sha256(content).hexdigest() != str(row["sha256"]):
                raise OwnerDataError("owner export image checksum does not match")
            suffix = source.suffix.lower() or ".bin"
            archive.writestr(
                f"images/{row['draft_id']}/v{row['version']}{suffix}",
                content,
            )


def _validate_scope(scope: str) -> None:
    if scope not in {"text", "image"}:
        raise ValueError("preference scope must be text or image")


def _preference(row: sqlite3.Row) -> PreferenceRule:
    return PreferenceRule(
        rule_id=int(row["id"]),
        scope=str(row["scope"]),
        rule=str(row["rule"]),
        version=int(row["version"]),
        active=bool(row["active"]),
        created_at=str(row["created_at"]),
    )


def _row_dict(
    row: sqlite3.Row,
    *,
    excluded: set[str] | None = None,
) -> dict[str, object]:
    hidden = excluded or set()
    values = dict(row)
    return {key: value for key, value in values.items() if key not in hidden}


def _microusd_to_usd(value: int) -> Decimal:
    return Decimal(value) / _MICRODOLLARS_PER_DOLLAR
