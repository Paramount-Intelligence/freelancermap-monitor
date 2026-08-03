from __future__ import annotations

import csv
import gzip
import hashlib
import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable, Iterator, Sequence

from config import Config
from parser import ProjectDetail, ProjectDiscovery
from utils import json_dumps, utc_now_iso

LOGGER = logging.getLogger(__name__)

DATABASE_PATH: Path = Config.DATABASE_PATH
SCHEMA_VERSION = 10

# SQLite's default host-parameter ceiling is commonly 999 on older builds.
# Keeping each dynamic IN clause below 900 preserves broad Python/SQLite
# compatibility while still making bulk state changes efficient.
_SQL_VARIABLE_CHUNK = 900
_ALLOWED_SYNCHRONOUS = {"FULL", "NORMAL"}
_ALLOWED_CHECKPOINT_MODES = {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


class DatabaseError(RuntimeError):
    """Base class for database-layer invariant failures."""


class DatabaseIdentityConflictError(DatabaseError):
    """A source key and URL resolve to two different project rows."""


class DatabaseInvariantError(DatabaseError):
    """Persisted state violates an application invariant."""


def _config_int(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = getattr(Config, name, default)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise DatabaseError(f"Config.{name} must be an integer, got {raw!r}.") from exc
    if not minimum <= value <= maximum:
        raise DatabaseError(
            f"Config.{name} must be between {minimum} and {maximum}, got {value}."
        )
    return value


def _database_synchronous() -> str:
    value = str(getattr(Config, "DATABASE_SYNCHRONOUS", "FULL")).strip().upper()
    if value not in _ALLOWED_SYNCHRONOUS:
        raise DatabaseError(
            "Config.DATABASE_SYNCHRONOUS must be FULL or NORMAL; "
            f"got {value!r}."
        )
    return value


def _open_connection() -> sqlite3.Connection:
    timeout_seconds = _config_int(
        "DATABASE_TIMEOUT_SECONDS",
        30,
        minimum=1,
        maximum=600,
    )
    conn = sqlite3.connect(
        str(DATABASE_PATH),
        timeout=timeout_seconds,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row

    busy_timeout_ms = _config_int(
        "DATABASE_BUSY_TIMEOUT_MS",
        15_000,
        minimum=0,
        maximum=600_000,
    )
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
    conn.execute(f"PRAGMA synchronous = {_database_synchronous()}")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA recursive_triggers = OFF")
    return conn


@contextmanager
def connection(*, write: bool = False) -> Iterator[sqlite3.Connection]:
    """Yield one transaction-scoped SQLite connection.

    Read transactions use ``BEGIN``. Mutation functions request ``write=True``,
    which uses ``BEGIN IMMEDIATE`` so competing writers wait at transaction
    start instead of failing halfway through an upsert or state transition.

    The public default remains a normal deferred transaction for compatibility
    with existing tests and maintenance scripts that call ``connection()`` and
    then issue their own UPDATE statements.
    """

    conn = _open_connection()
    try:
        conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield conn
        if conn.in_transaction:
            conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def initialize_database() -> None:
    """Create or transactionally migrate the local database.

    Index creation intentionally occurs *after* column migration. This allows a
    database created by an older monitor version to be upgraded even when the
    new indexes reference columns that did not exist in the old schema.
    """

    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = _open_connection()
    try:
        journal_mode = str(conn.execute("PRAGMA journal_mode = WAL").fetchone()[0])
        if journal_mode.casefold() != "wal":
            raise DatabaseError(
                f"Could not enable SQLite WAL mode for {DATABASE_PATH}; "
                f"SQLite returned {journal_mode!r}."
            )

        wal_autocheckpoint = _config_int(
            "DATABASE_WAL_AUTOCHECKPOINT_PAGES",
            1_000,
            minimum=1,
            maximum=1_000_000,
        )
        journal_size_limit = _config_int(
            "DATABASE_JOURNAL_SIZE_LIMIT_BYTES",
            64 * 1024 * 1024,
            minimum=1_048_576,
            maximum=2_147_483_647,
        )
        conn.execute(f"PRAGMA wal_autocheckpoint = {wal_autocheckpoint}")
        conn.execute(f"PRAGMA journal_size_limit = {journal_size_limit}")

        conn.execute("BEGIN IMMEDIATE")
        _create_tables(conn)
        _migrate_schema(conn)
        _backfill_normalized_fields(conn)
        _repair_observation_duplicates(conn)
        if _get_schema_version(conn) < 7:
            _repair_schema_v7(conn)
        if _get_schema_version(conn) < 8:
            _repair_schema_v8(conn)
        if _get_schema_version(conn) < 9:
            _repair_schema_v9(conn)
        if _get_schema_version(conn) < 10:
            _repair_schema_v10(conn)
        _create_indexes(conn)
        conn.execute(
            """
            INSERT INTO schema_migrations(version, applied_at)
            VALUES (?, ?)
            ON CONFLICT(version) DO NOTHING
            """,
            (SCHEMA_VERSION, utc_now_iso()),
        )
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()

        # PRAGMA optimize is designed as a low-cost maintenance hint. It is run
        # after the migration transaction, never in the middle of it.
        conn.execute("PRAGMA optimize")
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def _create_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_key TEXT NOT NULL UNIQUE,
            slug TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL UNIQUE,

            -- Exact normalized fields required by the assignment.
            scan_at TEXT NOT NULL,
            posted_at TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '',
            project_length TEXT NOT NULL DEFAULT '',
            budget TEXT NOT NULL DEFAULT '',
            engagement_type TEXT NOT NULL DEFAULT '',

            -- Rich detail-page fields retained for filtering and email output.
            company TEXT NOT NULL DEFAULT '',
            company_url TEXT NOT NULL DEFAULT '',
            contact_person TEXT NOT NULL DEFAULT '',
            city TEXT NOT NULL DEFAULT '',
            country TEXT NOT NULL DEFAULT '',
            workplace TEXT NOT NULL DEFAULT '',
            remote_percent TEXT NOT NULL DEFAULT '',
            contract_type TEXT NOT NULL DEFAULT '',
            duration TEXT NOT NULL DEFAULT '',
            start_date TEXT NOT NULL DEFAULT '',
            workload TEXT NOT NULL DEFAULT '',
            posted_text TEXT NOT NULL DEFAULT '',
            view_count INTEGER,
            publication_text TEXT NOT NULL DEFAULT '',
            published_at TEXT NOT NULL DEFAULT '',
            valid_through TEXT NOT NULL DEFAULT '',
            industry TEXT NOT NULL DEFAULT '',
            skills_json TEXT NOT NULL DEFAULT '[]',
            rate TEXT NOT NULL DEFAULT '',
            description_html TEXT NOT NULL DEFAULT '',
            application_url TEXT NOT NULL DEFAULT '',
            is_closed INTEGER NOT NULL DEFAULT 0 CHECK (is_closed IN (0, 1)),
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'closed', 'unknown')),

            -- Listing-card provenance. Card data is not discarded when a
            -- detail page is fetched.
            title_hint TEXT NOT NULL DEFAULT '',
            company_hint TEXT NOT NULL DEFAULT '',
            card_posted_text TEXT NOT NULL DEFAULT '',
            card_view_count INTEGER,
            card_description TEXT NOT NULL DEFAULT '',
            card_description_html TEXT NOT NULL DEFAULT '',
            card_location TEXT NOT NULL DEFAULT '',
            card_workplace TEXT NOT NULL DEFAULT '',
            card_contract_type TEXT NOT NULL DEFAULT '',
            card_duration TEXT NOT NULL DEFAULT '',
            card_start_date TEXT NOT NULL DEFAULT '',
            card_workload TEXT NOT NULL DEFAULT '',
            card_rate TEXT NOT NULL DEFAULT '',
            card_skills_json TEXT NOT NULL DEFAULT '[]',
            card_text TEXT NOT NULL DEFAULT '',
            card_hash TEXT NOT NULL DEFAULT '',
            last_card_changed_at TEXT,

            -- Raw evidence and parser metadata.
            raw_metadata_json TEXT NOT NULL DEFAULT '{}',
            raw_card_html_gzip BLOB,
            raw_html_gzip BLOB,
            content_hash TEXT NOT NULL DEFAULT '',

            -- Discovery/detail lifecycle.
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            first_detail_fetched_at TEXT,
            last_detail_fetched_at TEXT,
            detail_fetch_status TEXT NOT NULL DEFAULT 'pending'
                CHECK (detail_fetch_status IN ('pending', 'success', 'failed')),
            detail_fetch_error TEXT NOT NULL DEFAULT '',
            detail_fetch_attempts INTEGER NOT NULL DEFAULT 0
                CHECK (detail_fetch_attempts >= 0),
            detail_next_retry_at TEXT,

            -- Notification lifecycle.
            email_status TEXT NOT NULL DEFAULT 'pending'
                CHECK (email_status IN ('pending', 'sent', 'baseline')),
            emailed_at TEXT,
            email_attempts INTEGER NOT NULL DEFAULT 0
                CHECK (email_attempts >= 0),
            last_email_error TEXT NOT NULL DEFAULT '',
            last_email_message_id TEXT NOT NULL DEFAULT '',
            email_next_retry_at TEXT,
            baseline INTEGER NOT NULL DEFAULT 0 CHECK (baseline IN (0, 1)),

            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            discovered_count INTEGER NOT NULL DEFAULT 0,
            new_count INTEGER NOT NULL DEFAULT 0,
            detail_success_count INTEGER NOT NULL DEFAULT 0,
            detail_failure_count INTEGER NOT NULL DEFAULT 0,
            emailed_count INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS email_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL UNIQUE,
            project_ids_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'accepted',
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            accepted_at TEXT,
            sent_at TEXT,
            error TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS project_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            source TEXT NOT NULL CHECK (source IN ('card', 'detail')),
            content_hash TEXT NOT NULL,
            url TEXT NOT NULL DEFAULT '',
            parsed_json TEXT NOT NULL DEFAULT '{}',
            raw_html_gzip BLOB,
            captured_at TEXT NOT NULL,
            UNIQUE(project_id, source, content_hash)
        );

        CREATE TABLE IF NOT EXISTS project_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            posted_text TEXT NOT NULL DEFAULT '',
            view_count INTEGER,
            captured_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        );
        """
    )


def _migrate_schema(conn: sqlite3.Connection) -> None:
    # Comprehensive additive migration for every historical repository schema.
    # SQLite supports ADD COLUMN safely without rebuilding the table; normalized
    # values are backfilled in a separate step before the schema version moves.
    project_additions = {
        "slug": "TEXT NOT NULL DEFAULT ''",
        "scan_at": "TEXT NOT NULL DEFAULT ''",
        "posted_at": "TEXT NOT NULL DEFAULT ''",
        "title": "TEXT NOT NULL DEFAULT ''",
        "description": "TEXT NOT NULL DEFAULT ''",
        "location": "TEXT NOT NULL DEFAULT ''",
        "project_length": "TEXT NOT NULL DEFAULT ''",
        "budget": "TEXT NOT NULL DEFAULT ''",
        "engagement_type": "TEXT NOT NULL DEFAULT ''",
        "company": "TEXT NOT NULL DEFAULT ''",
        "company_url": "TEXT NOT NULL DEFAULT ''",
        "contact_person": "TEXT NOT NULL DEFAULT ''",
        "city": "TEXT NOT NULL DEFAULT ''",
        "country": "TEXT NOT NULL DEFAULT ''",
        "workplace": "TEXT NOT NULL DEFAULT ''",
        "remote_percent": "TEXT NOT NULL DEFAULT ''",
        "contract_type": "TEXT NOT NULL DEFAULT ''",
        "duration": "TEXT NOT NULL DEFAULT ''",
        "start_date": "TEXT NOT NULL DEFAULT ''",
        "workload": "TEXT NOT NULL DEFAULT ''",
        "posted_text": "TEXT NOT NULL DEFAULT ''",
        "view_count": "INTEGER",
        "publication_text": "TEXT NOT NULL DEFAULT ''",
        "published_at": "TEXT NOT NULL DEFAULT ''",
        "valid_through": "TEXT NOT NULL DEFAULT ''",
        "industry": "TEXT NOT NULL DEFAULT ''",
        "skills_json": "TEXT NOT NULL DEFAULT '[]'",
        "rate": "TEXT NOT NULL DEFAULT ''",
        "description_html": "TEXT NOT NULL DEFAULT ''",
        "application_url": "TEXT NOT NULL DEFAULT ''",
        "is_closed": "INTEGER NOT NULL DEFAULT 0",
        "status": "TEXT NOT NULL DEFAULT 'active'",
        "title_hint": "TEXT NOT NULL DEFAULT ''",
        "company_hint": "TEXT NOT NULL DEFAULT ''",
        "card_posted_text": "TEXT NOT NULL DEFAULT ''",
        "card_view_count": "INTEGER",
        "card_description": "TEXT NOT NULL DEFAULT ''",
        "card_description_html": "TEXT NOT NULL DEFAULT ''",
        "card_location": "TEXT NOT NULL DEFAULT ''",
        "card_workplace": "TEXT NOT NULL DEFAULT ''",
        "card_contract_type": "TEXT NOT NULL DEFAULT ''",
        "card_duration": "TEXT NOT NULL DEFAULT ''",
        "card_start_date": "TEXT NOT NULL DEFAULT ''",
        "card_workload": "TEXT NOT NULL DEFAULT ''",
        "card_rate": "TEXT NOT NULL DEFAULT ''",
        "card_skills_json": "TEXT NOT NULL DEFAULT '[]'",
        "card_text": "TEXT NOT NULL DEFAULT ''",
        "card_hash": "TEXT NOT NULL DEFAULT ''",
        "last_card_changed_at": "TEXT",
        "raw_metadata_json": "TEXT NOT NULL DEFAULT '{}'",
        "raw_card_html_gzip": "BLOB",
        "raw_html_gzip": "BLOB",
        "content_hash": "TEXT NOT NULL DEFAULT ''",
        "first_seen_at": "TEXT NOT NULL DEFAULT ''",
        "last_seen_at": "TEXT NOT NULL DEFAULT ''",
        "first_detail_fetched_at": "TEXT",
        "last_detail_fetched_at": "TEXT",
        "detail_fetch_status": "TEXT NOT NULL DEFAULT 'pending'",
        "detail_fetch_error": "TEXT NOT NULL DEFAULT ''",
        "detail_fetch_attempts": "INTEGER NOT NULL DEFAULT 0",
        "detail_next_retry_at": "TEXT",
        "email_status": "TEXT NOT NULL DEFAULT 'pending'",
        "emailed_at": "TEXT",
        "email_attempts": "INTEGER NOT NULL DEFAULT 0",
        "last_email_error": "TEXT NOT NULL DEFAULT ''",
        "last_email_message_id": "TEXT NOT NULL DEFAULT ''",
        "email_next_retry_at": "TEXT",
        "baseline": "INTEGER NOT NULL DEFAULT 0",
        "created_at": "TEXT NOT NULL DEFAULT ''",
        "updated_at": "TEXT NOT NULL DEFAULT ''",
    }
    for name, declaration in project_additions.items():
        _ensure_column(conn, "projects", name, declaration)

    scan_additions = {
        "finished_at": "TEXT",
        "status": "TEXT NOT NULL DEFAULT 'running'",
        "discovered_count": "INTEGER NOT NULL DEFAULT 0",
        "new_count": "INTEGER NOT NULL DEFAULT 0",
        "detail_success_count": "INTEGER NOT NULL DEFAULT 0",
        "detail_failure_count": "INTEGER NOT NULL DEFAULT 0",
        "emailed_count": "INTEGER NOT NULL DEFAULT 0",
        "error": "TEXT NOT NULL DEFAULT ''",
    }
    for name, declaration in scan_additions.items():
        _ensure_column(conn, "scans", name, declaration)

    email_batch_additions = {
        "project_ids_json": "TEXT NOT NULL DEFAULT '[]'",
        "status": "TEXT NOT NULL DEFAULT 'accepted'",
        "attempts": "INTEGER NOT NULL DEFAULT 0",
        "created_at": "TEXT NOT NULL DEFAULT ''",
        "updated_at": "TEXT NOT NULL DEFAULT ''",
        "accepted_at": "TEXT",
        "sent_at": "TEXT",
        "error": "TEXT NOT NULL DEFAULT ''",
    }
    for name, declaration in email_batch_additions.items():
        _ensure_column(conn, "email_batches", name, declaration)


def _repair_schema_v7(conn: sqlite3.Connection) -> None:
    """Repair data quality issues from earlier parser versions.

    - engagement_type: should be workload (not contract_type) when workload exists
    - location: repair leaked labels (e.g., "Remote Languages: English...")
    - requeue suspicious detail records for re-parsing
    - propagate corrected card values to normalized columns
    """
    # Fix engagement_type: prefer workload over contract_type
    conn.execute(
        """
        UPDATE projects SET
            engagement_type = CASE
                WHEN workload <> '' THEN workload
                WHEN contract_type <> '' THEN contract_type
                ELSE engagement_type
            END
        WHERE engagement_type <> ''
        """
    )

    # Requeue detail records with garbled locations BEFORE repairing labels,
    # so the requeue check can detect leaked labels that will be stripped next.
    conn.execute(
        """
        UPDATE projects SET
            detail_fetch_status = 'pending',
            detail_fetch_error = '',
            detail_fetch_attempts = 0,
            detail_next_retry_at = NULL
        WHERE detail_fetch_status = 'success'
          AND (
            location LIKE '%Report project%'
            OR location LIKE '%is unrealistic%'
            OR LENGTH(location) > 200
            OR location LIKE '%Languages:%'
            OR location LIKE '%Type:%'
            OR location LIKE '%Workload:%'
            OR location LIKE '%Workplace:%'
            OR location LIKE '%Contract:%'
            OR location LIKE '%Start date:%'
            OR location LIKE '%Budget:%'
          )
        """
    )

    # Repair malformed locations containing leaked labels
    _LEAKED_LABELS = (
        "Languages:", "Type:", "Duration:", "Rate:", "Workload:",
        "Workplace:", "Contract:", "Start date:", "Budget:",
        "Contact person:", "Description:",
    )
    for label in _LEAKED_LABELS:
        conn.execute(
            """
            UPDATE projects SET
                location = TRIM(SUBSTR(location, 1, INSTR(location, ?) - 1))
            WHERE location LIKE '%' || ? || '%'
              AND LENGTH(TRIM(SUBSTR(location, 1, INSTR(location, ?) - 1))) > 0
            """,
            (label, label, label),
        )

    # Propagate corrected card values to normalized columns
    conn.execute(
        """
        UPDATE projects SET
            location = CASE
                WHEN card_location <> '' AND (location = '' OR LOWER(location) IN ('not specified', 'n/a', 'unknown') OR location LIKE '%Report project%')
                THEN card_location
                ELSE location
            END,
            budget = CASE
                WHEN card_rate <> '' AND budget <> card_rate
                     AND (budget = '' OR budget LIKE '%Report project%')
                THEN card_rate
                ELSE budget
            END,
            engagement_type = CASE
                WHEN card_workload <> '' AND engagement_type <> card_workload
                THEN card_workload
                WHEN card_contract_type <> '' AND engagement_type <> card_contract_type
                     AND (engagement_type = '' OR engagement_type LIKE '%Report project%')
                THEN card_contract_type
                ELSE engagement_type
            END
        """
    )

    # Backfill project_length for rows where it's empty or garbled
    rows = conn.execute(
        """
        SELECT id, start_date, duration, workload,
               card_start_date, card_duration, card_workload, project_length
        FROM projects
        WHERE project_length = '' OR project_length LIKE '%Report project%'
        """
    ).fetchall()
    updates: list[tuple[str, int]] = []
    for row in rows:
        value = _project_length_text(
            _first(row["start_date"], row["card_start_date"]),
            _first(row["duration"], row["card_duration"]),
            _first(row["workload"], row["card_workload"]),
        )
        if value:
            updates.append((value, int(row["id"])))
    if updates:
        conn.executemany(
            "UPDATE projects SET project_length = ? WHERE id = ?",
            updates,
        )


def _repair_schema_v8(conn: sqlite3.Connection) -> None:
    """Repair 404/Error corrupted titles and locations from invalid detail fetches."""
    conn.execute(
        """
        UPDATE projects SET
            title = CASE WHEN title_hint <> '' THEN title_hint ELSE title END,
            location = CASE
                WHEN card_location <> '' AND (location = '' OR LOWER(location) IN ('not specified', 'n/a', 'unknown') OR location LIKE '%404%')
                THEN card_location
                ELSE location
            END,
            detail_fetch_status = 'failed'
        WHERE LOWER(title) LIKE '%404%'
           OR LOWER(title) LIKE '%not found%'
           OR LOWER(title) LIKE '%page does not exist%'
           OR LOWER(title) LIKE '%resource is gone%';
        """
    )


def _repair_schema_v9(conn: sqlite3.Connection) -> None:
    """Add dual-feed discovery sources and position provenance columns."""
    _ensure_column(conn, "projects", "discovery_sources_json", "TEXT NOT NULL DEFAULT '[\"primary_newest\"]'")
    _ensure_column(conn, "projects", "seen_in_primary", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(conn, "projects", "seen_in_personalized", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "projects", "primary_position", "INTEGER")
    _ensure_column(conn, "projects", "personalized_position", "INTEGER")


def _repair_schema_v10(conn: sqlite3.Connection) -> None:
    """Add per-scan feed status, degraded mode, and per-feed counts."""
    scan_additions = {
        "primary_feed_status": "TEXT NOT NULL DEFAULT ''",
        "personalized_feed_status": "TEXT NOT NULL DEFAULT ''",
        "degraded": "INTEGER NOT NULL DEFAULT 0",
        "degraded_reason": "TEXT NOT NULL DEFAULT ''",
        "primary_count": "INTEGER NOT NULL DEFAULT 0",
        "personalized_count": "INTEGER NOT NULL DEFAULT 0",
        "personalized_only_count": "INTEGER NOT NULL DEFAULT 0",
        "ignored_personalized_only_count": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, declaration in scan_additions.items():
        _ensure_column(conn, "scans", name, declaration)


def _ensure_column(
    conn: sqlite3.Connection,
    table: str,
    name: str,
    declaration: str,
) -> None:
    # table/name/declaration are internal constants, never user input.
    columns = {
        str(row["name"])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if name not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def _get_schema_version(conn: sqlite3.Connection) -> int:
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    except Exception:
        return 0


def _create_indexes(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_projects_email_ready
            ON projects(email_status, baseline, email_next_retry_at, posted_at DESC);
        CREATE INDEX IF NOT EXISTS idx_projects_detail_ready
            ON projects(detail_fetch_status, detail_next_retry_at, last_detail_fetched_at);
        CREATE INDEX IF NOT EXISTS idx_projects_last_seen
            ON projects(last_seen_at DESC);
        CREATE INDEX IF NOT EXISTS idx_projects_posted_at
            ON projects(posted_at DESC);
        CREATE INDEX IF NOT EXISTS idx_projects_published_at
            ON projects(published_at DESC);
        CREATE INDEX IF NOT EXISTS idx_projects_card_hash
            ON projects(card_hash);
        CREATE INDEX IF NOT EXISTS idx_projects_content_hash
            ON projects(content_hash);
        CREATE INDEX IF NOT EXISTS idx_scans_started_at
            ON scans(started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_snapshots_project_source
            ON project_snapshots(project_id, source, captured_at DESC);
        CREATE INDEX IF NOT EXISTS idx_observations_project_captured
            ON project_observations(project_id, captured_at DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_project_observations_value
            ON project_observations(
                project_id,
                posted_text,
                COALESCE(view_count, -1)
            );
        """
    )


def _backfill_normalized_fields(conn: sqlite3.Connection) -> None:
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE projects SET
            scan_at = COALESCE(NULLIF(scan_at, ''), NULLIF(first_seen_at, ''),
                               NULLIF(created_at, ''), ?),
            title = COALESCE(NULLIF(title, ''), NULLIF(title_hint, ''), ''),
            description = COALESCE(NULLIF(description, ''),
                                   NULLIF(card_description, ''),
                                   NULLIF(card_text, ''), ''),
            location = COALESCE(NULLIF(location, ''), NULLIF(card_location, ''), ''),
            budget = COALESCE(NULLIF(budget, ''), NULLIF(rate, ''),
                              NULLIF(card_rate, ''), ''),
            engagement_type = COALESCE(NULLIF(engagement_type, ''),
                                       NULLIF(contract_type, ''),
                                       NULLIF(card_contract_type, ''), ''),
            published_at = COALESCE(published_at, ''),
            posted_at = COALESCE(NULLIF(posted_at, ''), NULLIF(published_at, ''),
                                 NULLIF(scan_at, ''), NULLIF(first_seen_at, ''), ?),
            first_seen_at = COALESCE(NULLIF(first_seen_at, ''), NULLIF(scan_at, ''), ?),
            last_seen_at = COALESCE(NULLIF(last_seen_at, ''), NULLIF(scan_at, ''), ?),
            created_at = COALESCE(NULLIF(created_at, ''), NULLIF(scan_at, ''), ?),
            updated_at = COALESCE(NULLIF(updated_at, ''), NULLIF(scan_at, ''), ?)
        """,
        (now, now, now, now, now, now),
    )

    rows = conn.execute(
        """
        SELECT id, project_length, start_date, duration, workload,
               card_start_date, card_duration, card_workload
        FROM projects
        WHERE project_length = ''
        """
    ).fetchall()
    updates: list[tuple[str, int]] = []
    for row in rows:
        value = _project_length_text(
            _first(row["start_date"], row["card_start_date"]),
            _first(row["duration"], row["card_duration"]),
            _first(row["workload"], row["card_workload"]),
        )
        if value:
            updates.append((value, int(row["id"])))
    if updates:
        conn.executemany(
            "UPDATE projects SET project_length = ? WHERE id = ?",
            updates,
        )


def _repair_observation_duplicates(conn: sqlite3.Connection) -> None:
    # SQLite UNIQUE constraints treat NULL values as distinct. Older schema
    # versions could therefore accumulate duplicate observations where
    # view_count was NULL. Retain the earliest row before creating the
    # expression-based unique index.
    conn.execute(
        """
        DELETE FROM project_observations
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM project_observations
            GROUP BY project_id, posted_text, COALESCE(view_count, -1)
        )
        """
    )


def database_is_empty() -> bool:
    with connection() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]) == 0


def get_setting(key: str, default: str = "") -> str:
    key = str(key).strip()
    if not key:
        raise ValueError("Setting key must not be empty.")
    with connection() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            (key,),
        ).fetchone()
        return str(row["value"]) if row else default


def set_setting(key: str, value: str) -> None:
    key = str(key).strip()
    if not key:
        raise ValueError("Setting key must not be empty.")
    now = utc_now_iso()
    with connection(write=True) as conn:
        conn.execute(
            """
            INSERT INTO settings(key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, str(value), now),
        )


def baseline_initialized() -> bool:
    return get_setting("baseline_initialized", "false").strip().casefold() == "true"


def _parse_sources_json(raw: str) -> list[str]:
    """Parse a persisted discovery_sources_json value, tolerating corrupt rows."""
    if not raw or not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]


def upsert_discovery(
    project: Any,
    *,
    baseline: bool = False,
    seen_in_primary: bool = True,
    seen_in_personalized: bool = False,
    primary_position: int | None = None,
    personalized_position: int | None = None,
    discovery_sources: list[str] | None = None,
) -> tuple[int, bool]:
    """Insert or refresh one listing-card discovery atomically.

    ``scan_at`` is the first discovery timestamp and is never overwritten.
    ``last_seen_at`` records subsequent scans. Card and detail provenance stay
    separate, while normalized assignment fields are populated immediately from
    the card so a temporary detail-page failure never leaves an unusable row.
    """

    project = _adapt_discovery(project)
    now = utc_now_iso()
    scan_at = _first(getattr(project, "scan_at", ""), now)
    posted_at = _first(getattr(project, "posted_at", ""), scan_at)
    source_key = str(getattr(project, "source_key", "") or "").strip()
    url = str(getattr(project, "url", "") or "").strip()
    slug = str(getattr(project, "slug", "") or source_key).strip()

    sources = discovery_sources or (["primary_newest"] if seen_in_primary else [])
    if seen_in_personalized and "personalized_relevant" not in sources:
        sources.append("personalized_relevant")
    sources_json = json_dumps(sources)
    if not source_key:
        raise DatabaseInvariantError("Project discovery source_key is empty.")
    if not url:
        raise DatabaseInvariantError("Project discovery URL is empty.")

    raw_card = _compress_html(getattr(project, "card_html", ""))
    card_skills_json = json_dumps(list(getattr(project, "card_skills", []) or []))
    project_length = _project_length_text(
        getattr(project, "card_start_date", ""),
        getattr(project, "card_duration", ""),
        getattr(project, "card_workload", ""),
    )

    with connection(write=True) as conn:
        matches = conn.execute(
            "SELECT * FROM projects WHERE source_key = ? OR url = ? ORDER BY id",
            (source_key, url),
        ).fetchall()
        match_ids = {int(row["id"]) for row in matches}
        if len(match_ids) > 1:
            raise DatabaseIdentityConflictError(
                "Project source_key and URL resolve to different database rows: "
                f"source_key={source_key!r}, url={url!r}, ids={sorted(match_ids)}."
            )

        if matches:
            current = matches[0]
            project_id = int(current["id"])
            card_hash = str(getattr(project, "card_hash", "") or "")
            changed = bool(card_hash and card_hash != str(current["card_hash"] or ""))

            # Merge new discovery sources with the previously persisted ones so
            # repeated scans across both feeds never clobber earlier provenance.
            existing_sources = _parse_sources_json(
                str(current["discovery_sources_json"] or "")
            )
            merged_sources = list(existing_sources)
            for source in sources:
                if source not in merged_sources:
                    merged_sources.append(source)
            sources_json = json_dumps(merged_sources)

            resolved_source_key, resolved_slug, resolved_url = _identity_update_values(
                conn,
                project_id=project_id,
                current=current,
                source_key=source_key,
                slug=slug,
                url=url,
            )

            normalized_description = _merge_descriptions(
                str(current["description"] or ""),
                str(getattr(project, "card_description", "") or ""),
            )
            normalized_posted_at = _prefer_posted_at(
                candidate=posted_at,
                current=str(current["posted_at"] or ""),
                scan_at=str(current["scan_at"] or scan_at),
                detail_is_success=str(current["detail_fetch_status"]) == "success",
            )

            conn.execute(
                """
                UPDATE projects SET
                    source_key = ?, slug = ?, url = ?, last_seen_at = ?,
                    posted_at = ?,
                    title = CASE WHEN title = '' AND ? <> '' THEN ? ELSE title END,
                    description = CASE WHEN ? <> '' THEN ? ELSE description END,
                    location = CASE WHEN location = '' AND ? <> '' THEN ? ELSE location END,
                    project_length = CASE
                        WHEN project_length = '' AND ? <> '' THEN ? ELSE project_length END,
                    budget = CASE WHEN budget = '' AND ? <> '' THEN ? ELSE budget END,
                    engagement_type = CASE
                        WHEN engagement_type = '' AND ? <> '' THEN ? ELSE engagement_type END,
                    title_hint = CASE WHEN ? <> '' THEN ? ELSE title_hint END,
                    company_hint = CASE WHEN ? <> '' THEN ? ELSE company_hint END,
                    card_posted_text = CASE
                        WHEN ? <> '' THEN ? ELSE card_posted_text END,
                    card_view_count = COALESCE(?, card_view_count),
                    card_description = CASE
                        WHEN card_description = '' AND ? <> '' THEN ? ELSE card_description END,
                    card_description_html = CASE
                        WHEN card_description_html = '' AND ? <> '' THEN ? ELSE card_description_html END,
                    card_location = CASE
                        WHEN card_location = '' AND ? <> '' THEN ? ELSE card_location END,
                    card_workplace = CASE
                        WHEN card_workplace = '' AND ? <> '' THEN ? ELSE card_workplace END,
                    card_contract_type = CASE
                        WHEN card_contract_type = '' AND ? <> '' THEN ? ELSE card_contract_type END,
                    card_duration = CASE
                        WHEN card_duration = '' AND ? <> '' THEN ? ELSE card_duration END,
                    card_start_date = CASE
                        WHEN card_start_date = '' AND ? <> '' THEN ? ELSE card_start_date END,
                    card_workload = CASE
                        WHEN card_workload = '' AND ? <> '' THEN ? ELSE card_workload END,
                    card_rate = CASE WHEN ? <> '' THEN ? ELSE card_rate END,
                    card_skills_json = CASE
                        WHEN ? <> '[]' THEN ? ELSE card_skills_json END,
                    card_text = CASE WHEN ? <> '' THEN ? ELSE card_text END,
                    card_hash = CASE WHEN ? <> '' THEN ? ELSE card_hash END,
                    raw_card_html_gzip = COALESCE(?, raw_card_html_gzip),
                    last_card_changed_at = CASE
                        WHEN ? THEN ? ELSE last_card_changed_at END,
                    detail_fetch_status = CASE
                        WHEN ? THEN 'pending' ELSE detail_fetch_status END,
                    detail_fetch_attempts = CASE
                        WHEN ? THEN 0 ELSE detail_fetch_attempts END,
                    detail_fetch_error = CASE WHEN ? THEN '' ELSE detail_fetch_error END,
                    detail_next_retry_at = CASE
                        WHEN ? THEN NULL ELSE detail_next_retry_at END,
                    seen_in_primary = CASE WHEN ? THEN 1 ELSE seen_in_primary END,
                    seen_in_personalized = CASE WHEN ? THEN 1 ELSE seen_in_personalized END,
                    primary_position = COALESCE(?, primary_position),
                    personalized_position = COALESCE(?, personalized_position),
                    discovery_sources_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    resolved_source_key,
                    resolved_slug,
                    resolved_url,
                    scan_at,
                    normalized_posted_at,
                    project.title_hint,
                    project.title_hint,
                    normalized_description,
                    normalized_description,
                    project.card_location,
                    project.card_location,
                    project_length,
                    project_length,
                    project.card_rate,
                    project.card_rate,
                    project.card_contract_type,
                    project.card_contract_type,
                    project.title_hint,
                    project.title_hint,
                    project.company_hint,
                    project.company_hint,
                    project.posted_text,
                    project.posted_text,
                    project.view_count,
                    project.card_description,
                    project.card_description,
                    project.card_description_html,
                    project.card_description_html,
                    project.card_location,
                    project.card_location,
                    project.card_workplace,
                    project.card_workplace,
                    project.card_contract_type,
                    project.card_contract_type,
                    project.card_duration,
                    project.card_duration,
                    project.card_start_date,
                    project.card_start_date,
                    project.card_workload,
                    project.card_workload,
                    project.card_rate,
                    project.card_rate,
                    card_skills_json,
                    card_skills_json,
                    project.card_text,
                    project.card_text,
                    card_hash,
                    card_hash,
                    raw_card,
                    int(changed),
                    now,
                    int(changed),
                    int(changed),
                    int(changed),
                    int(changed),
                    int(seen_in_primary),
                    int(seen_in_personalized),
                    primary_position,
                    personalized_position,
                    sources_json,
                    now,
                    project_id,
                ),
            )

            _save_snapshot(
                conn,
                project_id=project_id,
                source="card",
                content_hash=card_hash,
                url=url,
                parsed_json=json_dumps(project.parsed_payload()),
                raw_html=getattr(project, "card_html", ""),
                captured_at=scan_at,
            )
            _save_observation(
                conn,
                project_id,
                project.posted_text,
                project.view_count,
                scan_at,
            )
            return project_id, False

        email_status = "baseline" if baseline else "pending"
        title = str(getattr(project, "title_hint", "") or "")
        description = str(getattr(project, "card_description", "") or "")
        location = str(getattr(project, "card_location", "") or "")
        budget = str(getattr(project, "card_rate", "") or "")
        engagement_type = str(getattr(project, "card_workload", "") or "") or str(getattr(project, "card_contract_type", "") or "")

        cursor = conn.execute(
            """
            INSERT INTO projects (
                source_key, slug, url,
                scan_at, posted_at, title, description, location,
                project_length, budget, engagement_type,
                company, workplace, contract_type, duration, start_date, workload,
                posted_text, view_count, skills_json, rate,
                title_hint, company_hint, card_posted_text, card_view_count,
                card_description, card_description_html, card_location, card_workplace,
                card_contract_type, card_duration, card_start_date, card_workload,
                card_rate, card_skills_json, card_text, card_hash,
                raw_card_html_gzip, last_card_changed_at,
                first_seen_at, last_seen_at,
                email_status, baseline, created_at, updated_at,
                discovery_sources_json, seen_in_primary, seen_in_personalized, primary_position, personalized_position
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?
            )
            """,
            (
                source_key,
                slug,
                url,
                scan_at,
                posted_at,
                title,
                description,
                location,
                project_length,
                budget,
                engagement_type,
                project.company_hint,
                project.card_workplace,
                project.card_contract_type,
                project.card_duration,
                project.card_start_date,
                project.card_workload,
                project.posted_text,
                project.view_count,
                card_skills_json,
                project.card_rate,
                project.title_hint,
                project.company_hint,
                project.posted_text,
                project.view_count,
                project.card_description,
                project.card_description_html,
                project.card_location,
                project.card_workplace,
                project.card_contract_type,
                project.card_duration,
                project.card_start_date,
                project.card_workload,
                project.card_rate,
                card_skills_json,
                project.card_text,
                project.card_hash,
                raw_card,
                scan_at,
                scan_at,
                scan_at,
                email_status,
                int(baseline),
                now,
                now,
                sources_json,
                int(seen_in_primary),
                int(seen_in_personalized),
                primary_position,
                personalized_position,
            ),
        )
        project_id = int(cursor.lastrowid)
        _save_snapshot(
            conn,
            project_id=project_id,
            source="card",
            content_hash=project.card_hash,
            url=url,
            parsed_json=json_dumps(project.parsed_payload()),
            raw_html=project.card_html,
            captured_at=scan_at,
        )
        _save_observation(
            conn,
            project_id,
            project.posted_text,
            project.view_count,
            scan_at,
        )
        return project_id, True


def _identity_update_values(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    current: sqlite3.Row,
    source_key: str,
    slug: str,
    url: str,
) -> tuple[str, str, str]:
    conflict = conn.execute(
        """
        SELECT id FROM projects
        WHERE id <> ? AND (source_key = ? OR url = ?)
        LIMIT 1
        """,
        (project_id, source_key, url),
    ).fetchone()
    if conflict:
        return str(current["source_key"]), str(current["slug"]), str(current["url"])
    return source_key, slug, url


def projects_needing_details(
    limit: int,
    include_existing_stale: bool = False,
) -> list[sqlite3.Row]:
    effective_limit = _positive_limit(limit, maximum=50_000)
    max_attempts = _config_int(
        "DETAIL_MAX_ATTEMPTS",
        5,
        minimum=1,
        maximum=100,
    )
    clauses = [
        "detail_fetch_status = 'pending'",
        """(
            detail_fetch_status = 'failed'
            AND detail_fetch_attempts < ?
            AND (detail_next_retry_at IS NULL OR datetime(detail_next_retry_at) <= datetime('now'))
        )""",
    ]
    params: list[Any] = [max_attempts]

    if include_existing_stale:
        refresh_hours = _config_int(
            "DETAIL_REFRESH_HOURS",
            24,
            minimum=1,
            maximum=24 * 365,
        )
        recent_hours = _config_int(
            "RECENTLY_SEEN_HOURS",
            72,
            minimum=1,
            maximum=24 * 365,
        )
        clauses.append(
            """(
                detail_fetch_status = 'success'
                AND datetime(last_detail_fetched_at) <= datetime('now', ?)
                AND datetime(last_seen_at) >= datetime('now', ?)
            )"""
        )
        params.extend([f"-{refresh_hours} hours", f"-{recent_hours} hours"])

    params.append(effective_limit)
    with connection() as conn:
        return conn.execute(
            f"""
            SELECT * FROM projects
            WHERE {' OR '.join(f'({clause})' for clause in clauses)}
            ORDER BY
                CASE detail_fetch_status
                    WHEN 'pending' THEN 0
                    WHEN 'failed' THEN 1
                    ELSE 2
                END,
                COALESCE(detail_next_retry_at, last_detail_fetched_at, first_seen_at) ASC,
                id ASC
            LIMIT ?
            """,
            params,
        ).fetchall()


def save_project_detail(
    project_id: int,
    detail: ProjectDetail,
    raw_html: str | None,
) -> None:
    """Persist a parsed detail page while preserving card provenance."""

    detail = _adapt_detail(detail)
    now = utc_now_iso()
    raw_blob = _compress_html(raw_html or "")

    with connection(write=True) as conn:
        current = conn.execute(
            "SELECT * FROM projects WHERE id = ?",
            (int(project_id),),
        ).fetchone()
        if not current:
            raise DatabaseInvariantError(f"Project row {project_id} no longer exists.")

        source_key = str(detail.source_key or current["source_key"])
        slug = str(detail.slug or current["slug"])
        url = str(detail.url or current["url"])
        source_key, slug, url = _identity_update_values(
            conn,
            project_id=int(project_id),
            current=current,
            source_key=source_key,
            slug=slug,
            url=url,
        )

        title = _first(detail.title, current["title_hint"], current["title"])
        company = _first(detail.company, current["company_hint"], current["company"])
        location = _first(detail.location, current["card_location"], current["location"])
        workplace = _first(detail.workplace, current["card_workplace"], current["workplace"])
        contract_type = _first(
            detail.contract_type,
            detail.engagement_type,
            current["card_contract_type"],
            current["contract_type"],
        )
        duration = _first(detail.duration, current["card_duration"], current["duration"])
        start_date = _first(
            detail.start_date,
            current["card_start_date"],
            current["start_date"],
        )
        workload = _first(detail.workload, current["card_workload"], current["workload"])
        posted_text = _first(
            detail.posted_text,
            current["card_posted_text"],
            current["posted_text"],
        )
        view_count = detail.view_count
        if view_count is None:
            view_count = current["card_view_count"]
        if view_count is None:
            view_count = current["view_count"]

        rate = _first(detail.rate, detail.budget, current["card_rate"], current["rate"])
        description = _merge_descriptions(
            str(current["card_description"] or ""),
            str(detail.description or current["description"] or ""),
        )
        description_html = _first(
            detail.description_html,
            current["card_description_html"],
            current["description_html"],
        )
        skills = (
            list(detail.skills or [])
            or _json_list(str(current["card_skills_json"] or "[]"))
            or _json_list(str(current["skills_json"] or "[]"))
        )
        project_length = _first(
            detail.project_length,
            _project_length_text(start_date, duration, workload),
            current["project_length"],
        )
        budget = _first(detail.budget, rate, current["budget"])
        engagement_type = _first(
            detail.workload,
            detail.engagement_type,
            contract_type,
            current["engagement_type"],
        )
        candidate_posted_at = _first(
            detail.posted_at,
            detail.published_at,
            current["posted_at"],
            current["scan_at"],
        )
        posted_at = _valid_timestamp_or_fallback(
            candidate_posted_at,
            str(current["scan_at"]),
        )
        published_at = _first(detail.published_at, current["published_at"])

        raw_metadata = dict(detail.raw_metadata or {})
        raw_metadata["parsed_canonical_url"] = detail.url
        if url != detail.url:
            raw_metadata["identity_collision_preserved_url"] = str(current["url"])

        conn.execute(
            """
            UPDATE projects SET
                source_key = ?, slug = ?, url = ?,
                posted_at = ?, title = ?, description = ?, location = ?,
                project_length = ?, budget = ?, engagement_type = ?,
                company = ?, company_url = ?, contact_person = ?, city = ?, country = ?,
                workplace = ?, remote_percent = ?, contract_type = ?, duration = ?,
                start_date = ?, workload = ?, posted_text = ?, view_count = ?,
                publication_text = ?, published_at = ?, valid_through = ?, industry = ?,
                skills_json = ?, rate = ?, description_html = ?, application_url = ?,
                is_closed = ?, status = ?, raw_metadata_json = ?,
                raw_html_gzip = COALESCE(?, raw_html_gzip), content_hash = ?,
                first_detail_fetched_at = COALESCE(first_detail_fetched_at, ?),
                last_detail_fetched_at = ?, detail_fetch_status = 'success',
                detail_fetch_error = '', detail_fetch_attempts = detail_fetch_attempts + 1,
                detail_next_retry_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (
                source_key,
                slug,
                url,
                posted_at,
                title,
                description,
                location,
                project_length,
                budget,
                engagement_type,
                company,
                detail.company_url,
                detail.contact_person,
                detail.city,
                detail.country,
                workplace,
                detail.remote_percent,
                contract_type,
                duration,
                start_date,
                workload,
                posted_text,
                view_count,
                detail.publication_text,
                published_at,
                detail.valid_through,
                detail.industry,
                json_dumps(skills),
                rate,
                description_html,
                detail.application_url,
                int(bool(detail.is_closed)),
                "closed" if detail.is_closed else "active",
                json_dumps(raw_metadata),
                raw_blob,
                detail.content_hash,
                now,
                now,
                now,
                int(project_id),
            ),
        )

        _save_snapshot(
            conn,
            project_id=int(project_id),
            source="detail",
            content_hash=detail.content_hash,
            url=detail.url,
            parsed_json=json_dumps(detail.parsed_payload()),
            raw_html=raw_html or "",
            captured_at=now,
        )
        _save_observation(
            conn,
            int(project_id),
            posted_text,
            view_count,
            now,
        )


def _save_snapshot(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    source: str,
    content_hash: str,
    url: str,
    parsed_json: str,
    raw_html: str,
    captured_at: str,
) -> None:
    if not content_hash:
        return
    raw_blob = _compress_html(raw_html)
    conn.execute(
        """
        INSERT INTO project_snapshots(
            project_id, source, content_hash, url,
            parsed_json, raw_html_gzip, captured_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(project_id, source, content_hash) DO NOTHING
        """,
        (
            project_id,
            source,
            content_hash,
            url,
            parsed_json,
            raw_blob,
            captured_at,
        ),
    )
    _prune_snapshots(conn, project_id, source)


def _save_observation(
    conn: sqlite3.Connection,
    project_id: int,
    posted_text: str,
    view_count: int | None,
    captured_at: str,
) -> None:
    if not posted_text and view_count is None:
        return
    conn.execute(
        """
        INSERT INTO project_observations(project_id, posted_text, view_count, captured_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT DO NOTHING
        """,
        (project_id, posted_text, view_count, captured_at),
    )
    _prune_observations(conn, project_id)


def _prune_snapshots(
    conn: sqlite3.Connection,
    project_id: int,
    source: str,
) -> None:
    keep = _config_int(
        "DATABASE_SNAPSHOTS_PER_SOURCE",
        25,
        minimum=1,
        maximum=10_000,
    )
    conn.execute(
        """
        DELETE FROM project_snapshots
        WHERE project_id = ? AND source = ? AND id NOT IN (
            SELECT id FROM project_snapshots
            WHERE project_id = ? AND source = ?
            ORDER BY captured_at DESC, id DESC
            LIMIT ?
        )
        """,
        (project_id, source, project_id, source, keep),
    )


def _prune_observations(conn: sqlite3.Connection, project_id: int) -> None:
    keep = _config_int(
        "DATABASE_OBSERVATIONS_PER_PROJECT",
        100,
        minimum=1,
        maximum=100_000,
    )
    conn.execute(
        """
        DELETE FROM project_observations
        WHERE project_id = ? AND id NOT IN (
            SELECT id FROM project_observations
            WHERE project_id = ?
            ORDER BY captured_at DESC, id DESC
            LIMIT ?
        )
        """,
        (project_id, project_id, keep),
    )


def mark_detail_failure(project_id: int, error: str) -> None:
    now_dt = datetime.now(timezone.utc)
    now = _iso_utc(now_dt)
    with connection(write=True) as conn:
        row = conn.execute(
            "SELECT detail_fetch_attempts FROM projects WHERE id = ?",
            (int(project_id),),
        ).fetchone()
        if not row:
            raise DatabaseInvariantError(f"Project row {project_id} no longer exists.")
        next_attempt = int(row["detail_fetch_attempts"] or 0) + 1
        next_retry = _iso_utc(now_dt + timedelta(seconds=_detail_retry_delay(next_attempt)))
        conn.execute(
            """
            UPDATE projects SET
                detail_fetch_status = 'failed',
                detail_fetch_error = ?,
                detail_fetch_attempts = ?,
                last_detail_fetched_at = ?,
                detail_next_retry_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (_clean_error(error), next_attempt, now, next_retry, now, int(project_id)),
        )


def _detail_retry_delay(attempt: int) -> int:
    base = _config_int(
        "DETAIL_RETRY_BASE_SECONDS",
        300,
        minimum=1,
        maximum=86_400,
    )
    maximum = _config_int(
        "DETAIL_RETRY_MAX_SECONDS",
        21_600,
        minimum=1,
        maximum=7 * 86_400,
    )
    return min(maximum, base * (2 ** max(0, min(attempt - 1, 16))))


def pending_email_projects(limit: int | None = None) -> list[sqlite3.Row]:
    configured_limit = _config_int(
        "MAX_EMAIL_PROJECTS_PER_MESSAGE",
        25,
        minimum=1,
        maximum=10_000,
    )
    effective_limit = _positive_limit(limit or configured_limit, maximum=10_000)
    max_detail_attempts = _config_int(
        "DETAIL_MAX_ATTEMPTS",
        5,
        minimum=1,
        maximum=100,
    )

    with connection() as conn:
        return conn.execute(
            """
            SELECT * FROM projects
            WHERE email_status = 'pending'
              AND baseline = 0
              AND (email_next_retry_at IS NULL OR datetime(email_next_retry_at) <= datetime('now'))
              AND (
                    detail_fetch_status = 'success'
                    OR (
                        detail_fetch_status = 'failed'
                        AND detail_fetch_attempts >= ?
                        AND COALESCE(NULLIF(title, ''), NULLIF(title_hint, '')) IS NOT NULL
                    )
              )
            ORDER BY datetime(posted_at) DESC, id DESC
            LIMIT ?
            """,
            (max_detail_attempts, effective_limit),
        ).fetchall()


def start_email_batch(project_ids: Sequence[int], message_id: str) -> None:
    ids = _normalize_project_ids(project_ids)
    if not ids:
        raise ValueError("Email batch requires at least one project ID.")
    clean_message_id = _clean_message_id(message_id)
    now = utc_now_iso()
    with connection(write=True) as conn:
        conn.execute(
            """
            INSERT INTO email_batches(
                message_id, project_ids_json, status, attempts,
                created_at, updated_at, accepted_at, error
            ) VALUES (?, ?, 'accepted', 1, ?, ?, ?, '')
            ON CONFLICT(message_id) DO UPDATE SET
                project_ids_json = excluded.project_ids_json,
                status = CASE
                    WHEN email_batches.status = 'sent' THEN 'sent'
                    ELSE 'accepted'
                END,
                attempts = CASE
                    WHEN email_batches.status = 'sent' THEN email_batches.attempts
                    ELSE email_batches.attempts + 1
                END,
                accepted_at = COALESCE(email_batches.accepted_at, excluded.accepted_at),
                updated_at = excluded.updated_at,
                error = ''
            """,
            (clean_message_id, json_dumps(ids), now, now, now),
        )


def mark_projects_emailed(
    project_ids: Sequence[int],
    message_id: str = "",
) -> None:
    ids = _normalize_project_ids(project_ids)
    if not ids:
        return
    clean_message_id = _clean_message_id(message_id, allow_empty=True)
    now = utc_now_iso()

    with connection(write=True) as conn:
        for chunk in _chunks(ids, _SQL_VARIABLE_CHUNK):
            placeholders = ",".join("?" for _ in chunk)
            conn.execute(
                f"""
                UPDATE projects SET
                    email_status = 'sent',
                    emailed_at = COALESCE(emailed_at, ?),
                    email_attempts = email_attempts +
                        CASE WHEN email_status = 'sent' THEN 0 ELSE 1 END,
                    last_email_error = '',
                    last_email_message_id = CASE
                        WHEN ? <> '' THEN ? ELSE last_email_message_id END,
                    email_next_retry_at = NULL,
                    updated_at = ?
                WHERE id IN ({placeholders}) AND baseline = 0
                """,
                [now, clean_message_id, clean_message_id, now, *chunk],
            )

        if clean_message_id:
            conn.execute(
                """
                UPDATE email_batches SET
                    status = 'sent', sent_at = COALESCE(sent_at, ?),
                    updated_at = ?, error = ''
                WHERE message_id = ?
                """,
                (now, now, clean_message_id),
            )


def mark_email_failure(
    project_ids: Sequence[int],
    error: str,
    message_id: str = "",
) -> None:
    ids = _normalize_project_ids(project_ids)
    if not ids:
        return
    clean_message_id = _clean_message_id(message_id, allow_empty=True)
    clean_error = _clean_error(error)
    now_dt = datetime.now(timezone.utc)
    now = _iso_utc(now_dt)

    with connection(write=True) as conn:
        rows: dict[int, int] = {}
        for chunk in _chunks(ids, _SQL_VARIABLE_CHUNK):
            placeholders = ",".join("?" for _ in chunk)
            for row in conn.execute(
                f"SELECT id, email_attempts FROM projects WHERE id IN ({placeholders})",
                chunk,
            ).fetchall():
                rows[int(row["id"])] = int(row["email_attempts"] or 0)

        for project_id in ids:
            attempt = rows.get(project_id, 0) + 1
            next_retry = _iso_utc(
                now_dt + timedelta(seconds=_email_retry_delay(attempt))
            )
            conn.execute(
                """
                UPDATE projects SET
                    email_attempts = ?, last_email_error = ?,
                    last_email_message_id = CASE
                        WHEN ? <> '' THEN ? ELSE last_email_message_id END,
                    email_next_retry_at = ?, updated_at = ?
                WHERE id = ? AND email_status = 'pending'
                """,
                (
                    attempt,
                    clean_error,
                    clean_message_id,
                    clean_message_id,
                    next_retry,
                    now,
                    project_id,
                ),
            )

        if clean_message_id:
            conn.execute(
                """
                INSERT INTO email_batches(
                    message_id, project_ids_json, status, attempts,
                    created_at, updated_at, error
                ) VALUES (?, ?, 'failed', 1, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    status = CASE
                        WHEN email_batches.status = 'sent' THEN 'sent'
                        ELSE 'failed'
                    END,
                    attempts = CASE
                        WHEN email_batches.status = 'sent' THEN email_batches.attempts
                        ELSE email_batches.attempts + 1
                    END,
                    updated_at = excluded.updated_at,
                    error = CASE
                        WHEN email_batches.status = 'sent' THEN email_batches.error
                        ELSE excluded.error
                    END
                """,
                (
                    clean_message_id,
                    json_dumps(ids),
                    now,
                    now,
                    clean_error,
                ),
            )


def _email_retry_delay(attempt: int) -> int:
    base = _config_int(
        "EMAIL_RETRY_BASE_SECONDS",
        300,
        minimum=1,
        maximum=86_400,
    )
    maximum = _config_int(
        "EMAIL_RETRY_MAX_SECONDS",
        21_600,
        minimum=1,
        maximum=7 * 86_400,
    )
    return min(maximum, base * (2 ** max(0, min(attempt - 1, 16))))


def mark_projects_as_baseline(project_ids: Sequence[int]) -> int:
    ids = _normalize_project_ids(project_ids)
    if not ids:
        return 0
    now = utc_now_iso()
    changed = 0
    with connection(write=True) as conn:
        for chunk in _chunks(ids, _SQL_VARIABLE_CHUNK):
            placeholders = ",".join("?" for _ in chunk)
            cursor = conn.execute(
                f"""
                UPDATE projects SET
                    baseline = 1,
                    email_status = CASE
                        WHEN email_status = 'sent' THEN 'sent' ELSE 'baseline' END,
                    email_next_retry_at = NULL,
                    updated_at = ?
                WHERE id IN ({placeholders}) AND baseline = 0
                """,
                [now, *chunk],
            )
            changed += max(0, int(cursor.rowcount))
    return changed


def mark_all_as_baseline() -> int:
    now = utc_now_iso()
    with connection(write=True) as conn:
        cursor = conn.execute(
            """
            UPDATE projects SET
                baseline = 1,
                email_status = CASE
                    WHEN email_status = 'sent' THEN 'sent' ELSE 'baseline' END,
                email_next_retry_at = NULL,
                updated_at = ?
            WHERE baseline = 0 AND email_status <> 'sent'
            """,
            (now,),
        )
        return max(0, int(cursor.rowcount))


def reset_baseline() -> int:
    """Void the current baseline so the next cycle re-baselines.

    Every project row is re-marked as baseline (sent rows keep their sent
    state) so no historical project can be emailed between the reset and
    the fresh baseline run. Settings are cleared atomically afterwards.

    Returns the number of rows re-marked.
    """
    now = utc_now_iso()
    with connection(write=True) as conn:
        cursor = conn.execute(
            """
            UPDATE projects SET
                baseline = 1,
                email_status = CASE
                    WHEN email_status = 'sent' THEN 'sent' ELSE 'baseline' END,
                email_next_retry_at = NULL,
                updated_at = ?
            WHERE email_status <> 'sent'
            """,
            (now,),
        )
        changed = max(0, int(cursor.rowcount))
    set_setting("baseline_initialized", "false")
    set_setting("baseline_initializing", "false")
    set_setting("baseline_started_at", "")
    set_setting("baseline_completed_at", "")
    return changed


def create_scan() -> int:
    with connection(write=True) as conn:
        cursor = conn.execute(
            "INSERT INTO scans(started_at, status) VALUES (?, 'running')",
            (utc_now_iso(),),
        )
        return int(cursor.lastrowid)


def finish_scan(scan_id: int, **values: Any) -> None:
    allowed = {
        "status",
        "discovered_count",
        "new_count",
        "detail_success_count",
        "detail_failure_count",
        "emailed_count",
        "error",
        "primary_feed_status",
        "personalized_feed_status",
        "degraded",
        "degraded_reason",
        "primary_count",
        "personalized_count",
        "personalized_only_count",
        "ignored_personalized_only_count",
    }
    clean = {key: value for key, value in values.items() if key in allowed}
    if "error" in clean:
        clean["error"] = _clean_error(str(clean["error"]), limit=8_000)
    if "degraded_reason" in clean:
        clean["degraded_reason"] = _clean_error(str(clean["degraded_reason"]), limit=2_000)
    for count_name in (
        "discovered_count",
        "new_count",
        "detail_success_count",
        "detail_failure_count",
        "emailed_count",
        "primary_count",
        "personalized_count",
        "personalized_only_count",
        "ignored_personalized_only_count",
    ):
        if count_name in clean:
            clean[count_name] = max(0, int(clean[count_name]))
    if "degraded" in clean:
        clean["degraded"] = 1 if bool(clean["degraded"]) else 0
    for status_name in ("primary_feed_status", "personalized_feed_status"):
        if status_name in clean:
            clean[status_name] = str(clean[status_name]).strip()[:32]
    clean["finished_at"] = utc_now_iso()

    assignments = ", ".join(f"{key} = ?" for key in clean)
    with connection(write=True) as conn:
        cursor = conn.execute(
            f"UPDATE scans SET {assignments} WHERE id = ?",
            [*clean.values(), int(scan_id)],
        )
        if cursor.rowcount != 1:
            raise DatabaseInvariantError(f"Scan row {scan_id} does not exist.")


def status_summary() -> dict[str, int]:
    with connection() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN baseline = 1 THEN 1 ELSE 0 END) AS baseline,
                SUM(CASE WHEN email_status = 'pending' THEN 1 ELSE 0 END) AS pending_email,
                SUM(CASE WHEN email_status = 'sent' THEN 1 ELSE 0 END) AS emailed,
                SUM(CASE WHEN detail_fetch_status = 'pending' THEN 1 ELSE 0 END) AS detail_pending,
                SUM(CASE WHEN detail_fetch_status = 'failed' THEN 1 ELSE 0 END) AS detail_failed,
                SUM(CASE WHEN is_closed = 1 THEN 1 ELSE 0 END) AS closed
            FROM projects
            """
        ).fetchone()
        snapshots = int(conn.execute("SELECT COUNT(*) FROM project_snapshots").fetchone()[0])
        observations = int(
            conn.execute("SELECT COUNT(*) FROM project_observations").fetchone()[0]
        )
        scans = int(conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0])
        batches = int(conn.execute("SELECT COUNT(*) FROM email_batches").fetchone()[0])

    result = {key: int(row[key] or 0) for key in row.keys()}
    result.update(
        snapshots=snapshots,
        observations=observations,
        scans=scans,
        email_batches=batches,
    )
    return result


def database_health() -> dict[str, Any]:
    issues: list[str] = []
    with connection() as conn:
        integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()]
        foreign_key_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
        foreign_keys_enabled = int(conn.execute("PRAGMA foreign_keys").fetchone()[0])
        required_columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(projects)").fetchall()
        }
        invalid_required = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM projects
                WHERE scan_at = '' OR posted_at = '' OR url = '' OR source_key = ''
                """
            ).fetchone()[0]
        )
        invalid_states = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM projects
                WHERE email_status NOT IN ('pending', 'sent', 'baseline')
                   OR detail_fetch_status NOT IN ('pending', 'success', 'failed')
                   OR baseline NOT IN (0, 1)
                   OR is_closed NOT IN (0, 1)
                """
            ).fetchone()[0]
        )

    expected_columns = {
        "scan_at",
        "posted_at",
        "title",
        "description",
        "location",
        "project_length",
        "url",
        "budget",
        "engagement_type",
    }
    missing_columns = sorted(expected_columns - required_columns)
    if integrity != ["ok"]:
        issues.append("SQLite integrity_check did not return 'ok'.")
    if foreign_key_rows:
        issues.append(f"SQLite reported {len(foreign_key_rows)} foreign-key violation(s).")
    if version != SCHEMA_VERSION:
        issues.append(
            f"Schema version is {version}, expected {SCHEMA_VERSION}; run initialize_database()."
        )
    if journal_mode.casefold() != "wal":
        issues.append(f"journal_mode is {journal_mode!r}, expected 'wal'.")
    if foreign_keys_enabled != 1:
        issues.append("Foreign-key enforcement is disabled on the health-check connection.")
    if missing_columns:
        issues.append("Missing required project columns: " + ", ".join(missing_columns))
    if invalid_required:
        issues.append(f"{invalid_required} project row(s) have empty identity/timestamp fields.")
    if invalid_states:
        issues.append(f"{invalid_states} project row(s) have invalid lifecycle state values.")

    return {
        "ok": not issues,
        "integrity": integrity,
        "foreign_key_errors": len(foreign_key_rows),
        "schema_version": version,
        "journal_mode": journal_mode,
        "required_columns_present": not missing_columns,
        "invalid_required_rows": invalid_required,
        "invalid_state_rows": invalid_states,
        "issues": issues,
        "sqlite_version": sqlite3.sqlite_version,
    }


def backup_database(destination: Path) -> None:
    """Create, verify, and atomically publish a consistent SQLite backup."""

    source = Path(DATABASE_PATH).expanduser().resolve(strict=False)
    destination = Path(destination).expanduser().resolve(strict=False)
    if source == destination:
        raise ValueError("Backup destination must differ from the active database file.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    temporary.unlink(missing_ok=True)

    source_conn = _open_connection()
    target_conn = sqlite3.connect(str(temporary), isolation_level=None)
    try:
        source_conn.backup(target_conn, pages=256, sleep=0.05)
        target_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        check = [str(row[0]) for row in target_conn.execute("PRAGMA integrity_check").fetchall()]
        if check != ["ok"]:
            raise DatabaseError(f"Backup integrity check failed: {check}")
    finally:
        target_conn.close()
        source_conn.close()

    try:
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def checkpoint_database(mode: str = "PASSIVE") -> tuple[int, int, int]:
    normalized = str(mode).strip().upper()
    if normalized not in _ALLOWED_CHECKPOINT_MODES:
        raise ValueError(
            "Checkpoint mode must be one of: " + ", ".join(sorted(_ALLOWED_CHECKPOINT_MODES))
        )
    conn = _open_connection()
    try:
        row = conn.execute(f"PRAGMA wal_checkpoint({normalized})").fetchone()
        return int(row[0]), int(row[1]), int(row[2])
    finally:
        conn.close()


def optimize_database() -> None:
    conn = _open_connection()
    try:
        conn.execute("PRAGMA optimize")
    finally:
        conn.close()


def recent_scans(limit: int = 20) -> list[sqlite3.Row]:
    effective_limit = _positive_limit(limit, maximum=10_000)
    with connection() as conn:
        return conn.execute(
            "SELECT * FROM scans ORDER BY id DESC LIMIT ?",
            (effective_limit,),
        ).fetchall()


def list_projects(limit: int = 20) -> list[sqlite3.Row]:
    effective_limit = _positive_limit(limit, maximum=100_000)
    with connection() as conn:
        return conn.execute(
            """
            SELECT
                id, scan_at, posted_at, title, title_hint,
                description, location, project_length, url,
                budget, engagement_type,
                company, company_hint, workplace, contract_type,
                duration, start_date, workload, published_at,
                email_status, detail_fetch_status, baseline,
                first_seen_at, last_seen_at
            FROM projects
            ORDER BY id DESC
            LIMIT ?
            """,
            (effective_limit,),
        ).fetchall()


def export_csv(path: Path) -> int:
    """Export project rows without raw HTML and neutralize spreadsheet formulas."""

    destination = Path(path).expanduser().resolve(strict=False)
    with connection() as conn:
        rows = conn.execute("SELECT * FROM projects ORDER BY id ASC").fetchall()

    destination.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        destination.write_text("", encoding="utf-8-sig")
        return 0

    excluded = {"raw_html_gzip", "raw_card_html_gzip"}
    columns = [name for name in rows[0].keys() if name not in excluded]
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({column: _csv_safe(row[column]) for column in columns})
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return len(rows)


def reset_failed_details() -> int:
    now = utc_now_iso()
    with connection(write=True) as conn:
        cursor = conn.execute(
            """
            UPDATE projects SET
                detail_fetch_status = 'pending',
                detail_fetch_error = '',
                detail_fetch_attempts = 0,
                detail_next_retry_at = NULL,
                updated_at = ?
            WHERE detail_fetch_status = 'failed'
            """,
            (now,),
        )
        return max(0, int(cursor.rowcount))



def _adapt_discovery(project: ProjectDiscovery) -> SimpleNamespace:
    """Normalize old and new parser dataclasses to one database contract."""

    finalizer = getattr(project, "finalize", None)
    if callable(finalizer):
        finalized = finalizer()
        if finalized is not None:
            project = finalized

    now = utc_now_iso()
    values: dict[str, Any] = {
        "source_key": str(getattr(project, "source_key", "") or ""),
        "slug": str(getattr(project, "slug", "") or ""),
        "url": str(getattr(project, "url", "") or ""),
        "title_hint": str(getattr(project, "title_hint", "") or ""),
        "company_hint": str(getattr(project, "company_hint", "") or ""),
        "posted_text": str(getattr(project, "posted_text", "") or ""),
        "view_count": getattr(project, "view_count", None),
        "card_description": str(getattr(project, "card_description", "") or ""),
        "card_description_html": str(
            getattr(project, "card_description_html", "") or ""
        ),
        "card_location": str(getattr(project, "card_location", "") or ""),
        "card_workplace": str(getattr(project, "card_workplace", "") or ""),
        "card_contract_type": str(
            getattr(project, "card_contract_type", "") or ""
        ),
        "card_duration": str(getattr(project, "card_duration", "") or ""),
        "card_start_date": str(getattr(project, "card_start_date", "") or ""),
        "card_workload": str(getattr(project, "card_workload", "") or ""),
        "card_rate": str(getattr(project, "card_rate", "") or ""),
        "card_skills": list(getattr(project, "card_skills", []) or []),
        "card_text": str(getattr(project, "card_text", "") or ""),
        "card_html": str(getattr(project, "card_html", "") or ""),
        "scan_at": str(getattr(project, "scan_at", "") or now),
        "posted_at": str(getattr(project, "posted_at", "") or ""),
        "card_hash": str(getattr(project, "card_hash", "") or ""),
    }
    values["slug"] = values["slug"] or values["source_key"]
    values["posted_at"] = values["posted_at"] or values["scan_at"]
    if not values["card_description"] and values["card_text"]:
        # Legacy parser versions exposed only card_text. Preserve it as raw
        # card evidence, but avoid pretending the entire card is a clean
        # description when a richer parser is available.
        values["card_description"] = values["card_text"]

    if not values["card_hash"]:
        hash_payload = {
            key: values[key]
            for key in (
                "title_hint",
                "company_hint",
                "card_description",
                "card_location",
                "card_workplace",
                "card_contract_type",
                "card_duration",
                "card_start_date",
                "card_workload",
                "card_rate",
                "card_skills",
            )
        }
        values["card_hash"] = hashlib.sha256(
            json.dumps(
                hash_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    payload_method = getattr(project, "parsed_payload", None)
    if callable(payload_method):
        payload = payload_method()
    else:
        payload = {key: value for key, value in values.items() if key != "card_html"}
    values["parsed_payload"] = lambda: payload
    return SimpleNamespace(**values)


def _adapt_detail(detail: ProjectDetail) -> SimpleNamespace:
    """Normalize parser detail models across repository versions."""

    finalizer = getattr(detail, "finalize", None)
    if callable(finalizer):
        finalized = finalizer()
        if finalized is not None:
            detail = finalized

    now = utc_now_iso()
    names_with_empty_default = (
        "source_key",
        "slug",
        "url",
        "title",
        "company",
        "company_url",
        "contact_person",
        "location",
        "city",
        "country",
        "workplace",
        "remote_percent",
        "contract_type",
        "duration",
        "start_date",
        "workload",
        "posted_text",
        "publication_text",
        "published_at",
        "valid_through",
        "industry",
        "rate",
        "description",
        "description_html",
        "application_url",
        "content_hash",
        "scan_at",
        "posted_at",
        "project_length",
        "budget",
        "engagement_type",
    )
    values: dict[str, Any] = {
        name: str(getattr(detail, name, "") or "")
        for name in names_with_empty_default
    }
    values.update(
        view_count=getattr(detail, "view_count", None),
        skills=list(getattr(detail, "skills", []) or []),
        raw_metadata=dict(getattr(detail, "raw_metadata", {}) or {}),
        is_closed=bool(getattr(detail, "is_closed", False)),
    )
    values["slug"] = values["slug"] or values["source_key"]
    values["scan_at"] = values["scan_at"] or now
    values["posted_at"] = (
        values["posted_at"] or values["published_at"] or values["scan_at"]
    )
    values["project_length"] = values["project_length"] or _project_length_text(
        values["start_date"], values["duration"], values["workload"]
    )
    values["budget"] = values["budget"] or values["rate"]
    values["engagement_type"] = (
        values["engagement_type"] or values["contract_type"]
    )
    if not values["content_hash"]:
        content_payload = {
            key: values[key]
            for key in (
                "title",
                "company",
                "location",
                "workplace",
                "contract_type",
                "duration",
                "start_date",
                "workload",
                "posted_at",
                "rate",
                "description",
                "is_closed",
            )
        }
        values["content_hash"] = hashlib.sha256(
            json.dumps(
                content_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    payload_method = getattr(detail, "parsed_payload", None)
    if callable(payload_method):
        payload = payload_method()
    else:
        payload = dict(values)
    values["parsed_payload"] = lambda: payload
    return SimpleNamespace(**values)

def _compress_html(value: str) -> bytes | None:
    if not value or not bool(getattr(Config, "STORE_RAW_HTML", True)):
        return None
    maximum = _config_int(
        "DATABASE_MAX_RAW_HTML_BYTES",
        5_000_000,
        minimum=10_000,
        maximum=100_000_000,
    )
    raw = value.encode("utf-8", errors="replace")
    if len(raw) > maximum:
        LOGGER.warning(
            "Raw HTML exceeded DATABASE_MAX_RAW_HTML_BYTES=%d and was truncated before compression.",
            maximum,
        )
        raw = raw[:maximum].decode("utf-8", errors="ignore").encode("utf-8")
    return gzip.compress(raw, compresslevel=6, mtime=0)


def _first(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return ""


def _json_list(raw: str) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


def _project_length_text(start_date: Any, duration: Any, workload: Any) -> str:
    parts: list[str] = []
    for label, value in (
        ("Start", start_date),
        ("Duration", duration),
        ("Workload", workload),
    ):
        text = " ".join(str(value or "").split())
        if text:
            parts.append(f"{label}: {text}")
    return " | ".join(parts)


def _merge_descriptions(first: str, second: str) -> str:
    """Combine card/detail text without repeating identical paragraphs."""

    paragraphs: list[str] = []
    fingerprints: list[str] = []
    for raw in (first, second):
        for paragraph in str(raw or "").replace("\r", "\n").split("\n"):
            cleaned = " ".join(paragraph.split())
            if not cleaned:
                continue
            fingerprint = cleaned.casefold().strip(" .:;-_")
            if not fingerprint:
                continue
            # Skip exact duplicates and a short card paragraph already fully
            # contained in the richer detail text (or vice versa).
            if any(
                fingerprint == existing
                or (len(fingerprint) >= 80 and fingerprint in existing)
                or (len(existing) >= 80 and existing in fingerprint)
                for existing in fingerprints
            ):
                continue
            paragraphs.append(cleaned)
            fingerprints.append(fingerprint)
    return "\n\n".join(paragraphs)


def _prefer_posted_at(
    *,
    candidate: str,
    current: str,
    scan_at: str,
    detail_is_success: bool,
) -> str:
    candidate_value = _valid_timestamp_or_fallback(candidate, scan_at)
    current_value = _valid_timestamp_or_fallback(current, scan_at)
    if detail_is_success:
        return current_value
    # Relative labels may drift by seconds across scans. Preserve the earliest
    # plausible estimate until the detail page supplies a stronger timestamp.
    try:
        return min(
            _parse_iso_datetime(candidate_value),
            _parse_iso_datetime(current_value),
        ).isoformat(timespec="seconds")
    except ValueError:
        return current_value


def _valid_timestamp_or_fallback(value: Any, fallback: str) -> str:
    try:
        return _parse_iso_datetime(str(value)).isoformat(timespec="seconds")
    except ValueError:
        try:
            return _parse_iso_datetime(str(fallback)).isoformat(timespec="seconds")
        except ValueError:
            return utc_now_iso()


def _parse_iso_datetime(value: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("empty timestamp")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _normalize_project_ids(values: Iterable[int]) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()
    for raw in values:
        value = int(raw)
        if value <= 0:
            raise ValueError(f"Project ID must be positive, got {value}.")
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _chunks(values: Sequence[int], size: int) -> Iterator[list[int]]:
    for start in range(0, len(values), size):
        yield list(values[start : start + size])


def _positive_limit(value: Any, *, maximum: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Limit must be an integer, got {value!r}.") from exc
    if result < 1:
        raise ValueError("Limit must be at least 1.")
    return min(result, maximum)


def _clean_message_id(value: str, *, allow_empty: bool = False) -> str:
    result = str(value or "").strip()
    if not result and allow_empty:
        return ""
    if not result:
        raise ValueError("Message-ID must not be empty.")
    if "\r" in result or "\n" in result:
        raise ValueError("Message-ID must not contain newlines.")
    if len(result) > 998:
        raise ValueError("Message-ID exceeds the RFC header line limit.")
    return result


def _clean_error(value: Any, *, limit: int = 2_000) -> str:
    return " ".join(str(value or "").replace("\x00", " ").split())[:limit]


def _csv_safe(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return value
    text = str(value)
    stripped = text.lstrip()
    if stripped.startswith(_CSV_FORMULA_PREFIXES):
        return "'" + text
    return text