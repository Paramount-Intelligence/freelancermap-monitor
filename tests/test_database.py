from __future__ import annotations

import csv
import gzip
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database
from config import Config
from parser import ProjectDetail, ProjectDiscovery


BASE = "https://www.freelancermap.com"
SCAN = "2026-07-31T00:00:00+00:00"


class EnhancedDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.tempdir.name) / "test.db"
        database.initialize_database()

    def tearDown(self):
        database.DATABASE_PATH = self.original_path
        self.tempdir.cleanup()

    @staticmethod
    def discovery(slug: str = "example", **overrides):
        values = {
            "source_key": slug,
            "slug": slug,
            "url": f"{BASE}/project/{slug}",
            "title_hint": "Senior Python Engineer",
            "company_hint": "Example GmbH",
            "posted_text": "3 hours ago",
            "card_description": "Card summary with delivery requirements.",
            "card_location": "Berlin / Remote",
            "card_contract_type": "Freelance",
            "card_duration": "6 months",
            "card_start_date": "ASAP",
            "card_workload": "Full-time",
            "card_rate": "€700/day",
            "card_html": "<article>Card summary with delivery requirements.</article>",
            "scan_at": SCAN,
        }
        values.update(overrides)
        return ProjectDiscovery(**values).finalize()

    def test_required_assignment_fields_are_present_and_populated_from_card(self):
        project_id, created = database.upsert_discovery(self.discovery())
        self.assertTrue(created)
        with database.connection() as conn:
            row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        self.assertEqual(SCAN, row["scan_at"])
        self.assertTrue(row["posted_at"])
        self.assertEqual("Senior Python Engineer", row["title"])
        self.assertIn("Card summary", row["description"])
        self.assertEqual("Berlin / Remote", row["location"])
        self.assertEqual("Start: ASAP | Duration: 6 months | Workload: Full-time", row["project_length"])
        self.assertEqual("€700/day", row["budget"])
        self.assertEqual("Full-time", row["engagement_type"])
        self.assertEqual(f"{BASE}/project/example", row["url"])

    def test_posted_at_falls_back_to_scan_at(self):
        project_id, _ = database.upsert_discovery(
            self.discovery(posted_text="", posted_at="")
        )
        with database.connection() as conn:
            row = conn.execute("SELECT scan_at, posted_at FROM projects WHERE id = ?", (project_id,)).fetchone()
        self.assertEqual(row["scan_at"], row["posted_at"])

    def test_url_and_source_key_deduplicate_atomically(self):
        first_id, first_created = database.upsert_discovery(self.discovery())
        second_id, second_created = database.upsert_discovery(self.discovery())
        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first_id, second_id)
        self.assertEqual(1, database.status_summary()["total"])

    def test_split_identity_collision_is_rejected_instead_of_corrupting_rows(self):
        database.upsert_discovery(self.discovery("one"))
        database.upsert_discovery(self.discovery("two"))
        conflicting = self.discovery(
            "one",
            url=f"{BASE}/project/two",
        )
        with self.assertRaises(database.DatabaseIdentityConflictError):
            database.upsert_discovery(conflicting)

    def test_detail_preserves_card_provenance_and_combines_unique_description(self):
        project_id, _ = database.upsert_discovery(self.discovery())
        detail = ProjectDetail(
            source_key="example",
            slug="example",
            url=f"{BASE}/project/example",
            title="Senior Python Engineer",
            description="Full detail text with architecture and delivery scope.",
            location="Berlin",
            contract_type="Freelance",
            duration="6 months",
            start_date="ASAP",
            workload="Full-time",
            rate="€700/day",
            scan_at=SCAN,
        ).finalize()
        # Raw HTML retention is privacy-safe-off by default; this test
        # explicitly opts in to verify the opt-in storage pipeline.
        with patch.object(Config, "STORE_RAW_HTML", True):
            database.save_project_detail(project_id, detail, "<main>detail</main>")
        with database.connection() as conn:
            row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            snapshots = conn.execute(
                "SELECT source, COUNT(*) c FROM project_snapshots WHERE project_id = ? GROUP BY source",
                (project_id,),
            ).fetchall()
        self.assertIn("Card summary", row["description"])
        self.assertIn("Full detail text", row["description"])
        self.assertEqual("Card summary with delivery requirements.", row["card_description"])
        self.assertEqual("success", row["detail_fetch_status"])
        self.assertEqual({"card": 1, "detail": 1}, {r["source"]: r["c"] for r in snapshots})
        self.assertEqual("<main>detail</main>", gzip.decompress(row["raw_html_gzip"]).decode())

    def test_material_card_change_requeues_detail_but_view_change_does_not(self):
        original = self.discovery(view_count=1)
        project_id, _ = database.upsert_discovery(original)
        database.save_project_detail(
            project_id,
            ProjectDetail(
                "example", "example", f"{BASE}/project/example",
                title="Senior Python Engineer", description="Detail", scan_at=SCAN,
            ).finalize(),
            None,
        )
        database.upsert_discovery(self.discovery(view_count=2))
        with database.connection() as conn:
            status = conn.execute("SELECT detail_fetch_status FROM projects WHERE id = ?", (project_id,)).fetchone()[0]
        self.assertEqual("success", status)

        database.upsert_discovery(self.discovery(card_description="Materially changed card text."))
        with database.connection() as conn:
            status = conn.execute("SELECT detail_fetch_status FROM projects WHERE id = ?", (project_id,)).fetchone()[0]
        self.assertEqual("pending", status)

    def test_detail_failure_uses_bounded_retry_schedule(self):
        project_id, _ = database.upsert_discovery(self.discovery())
        with patch.object(Config, "DETAIL_RETRY_BASE_SECONDS", 60, create=True), patch.object(
            Config, "DETAIL_RETRY_MAX_SECONDS", 600, create=True
        ):
            database.mark_detail_failure(project_id, "temporary error")
        with database.connection() as conn:
            row = conn.execute(
                "SELECT detail_fetch_status, detail_fetch_attempts, detail_next_retry_at FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
        self.assertEqual("failed", row["detail_fetch_status"])
        self.assertEqual(1, row["detail_fetch_attempts"])
        self.assertTrue(row["detail_next_retry_at"])

    def test_exhausted_detail_failure_can_still_send_card_alert(self):
        project_id, _ = database.upsert_discovery(self.discovery())
        with database.connection() as conn:
            conn.execute(
                "UPDATE projects SET detail_fetch_status='failed', detail_fetch_attempts=? WHERE id=?",
                (Config.DETAIL_MAX_ATTEMPTS, project_id),
            )
        rows = database.pending_email_projects()
        self.assertEqual([project_id], [row["id"] for row in rows])

    def test_mark_emailed_is_idempotent(self):
        project_id, _ = database.upsert_discovery(self.discovery())
        database.save_project_detail(
            project_id,
            ProjectDetail("example", "example", f"{BASE}/project/example", title="Title", scan_at=SCAN).finalize(),
            None,
        )
        database.start_email_batch([project_id], "<batch@example.com>")
        database.mark_projects_emailed([project_id], "<batch@example.com>")
        database.mark_projects_emailed([project_id], "<batch@example.com>")
        with database.connection() as conn:
            row = conn.execute("SELECT email_status, email_attempts FROM projects WHERE id=?", (project_id,)).fetchone()
        self.assertEqual("sent", row["email_status"])
        self.assertEqual(1, row["email_attempts"])

    def test_observation_with_null_view_count_is_deduplicated(self):
        item = self.discovery(view_count=None)
        project_id, _ = database.upsert_discovery(item)
        database.upsert_discovery(item)
        with database.connection() as conn:
            count = conn.execute("SELECT COUNT(*) FROM project_observations WHERE project_id=?", (project_id,)).fetchone()[0]
        self.assertEqual(1, count)

    def test_csv_export_neutralizes_formula_injection(self):
        project_id, _ = database.upsert_discovery(self.discovery(title_hint="=HYPERLINK(\"bad\")"))
        destination = Path(self.tempdir.name) / "projects.csv"
        database.export_csv(destination)
        with destination.open("r", newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual("'=HYPERLINK(\"bad\")", rows[0]["title"])
        self.assertEqual(project_id, int(rows[0]["id"]))

    def test_backup_is_verified_and_health_reports_required_schema(self):
        database.upsert_discovery(self.discovery())
        destination = Path(self.tempdir.name) / "backup.db"
        database.backup_database(destination)
        self.assertTrue(destination.exists())
        conn = sqlite3.connect(destination)
        try:
            self.assertEqual("ok", conn.execute("PRAGMA integrity_check").fetchone()[0])
        finally:
            conn.close()
        health = database.database_health()
        self.assertTrue(health["ok"], health["issues"])
        self.assertEqual(database.SCHEMA_VERSION, health["schema_version"])
        self.assertTrue(health["required_columns_present"])

    def test_old_schema_is_migrated_without_data_loss(self):
        database.DATABASE_PATH.unlink(missing_ok=True)
        conn = sqlite3.connect(database.DATABASE_PATH)
        conn.executescript(
            """
            CREATE TABLE projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL UNIQUE,
                slug TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL DEFAULT '',
                company TEXT NOT NULL DEFAULT '',
                location TEXT NOT NULL DEFAULT '',
                contract_type TEXT NOT NULL DEFAULT '',
                duration TEXT NOT NULL DEFAULT '',
                start_date TEXT NOT NULL DEFAULT '',
                rate TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                title_hint TEXT NOT NULL DEFAULT '',
                card_text TEXT NOT NULL DEFAULT '',
                raw_metadata_json TEXT NOT NULL DEFAULT '{}',
                raw_html_gzip BLOB,
                content_hash TEXT NOT NULL DEFAULT '',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                detail_fetch_status TEXT NOT NULL DEFAULT 'pending',
                detail_fetch_error TEXT NOT NULL DEFAULT '',
                detail_fetch_attempts INTEGER NOT NULL DEFAULT 0,
                email_status TEXT NOT NULL DEFAULT 'pending',
                email_attempts INTEGER NOT NULL DEFAULT 0,
                last_email_error TEXT NOT NULL DEFAULT '',
                baseline INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE scans (id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL);
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
            """
        )
        conn.execute(
            """
            INSERT INTO projects(
                source_key, slug, url, title, location, contract_type, duration,
                start_date, rate, description, first_seen_at, last_seen_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy", "legacy", f"{BASE}/project/legacy", "Legacy Title",
                "Remote", "Freelance", "3 months", "ASAP", "€500/day",
                "Legacy description", SCAN, SCAN, SCAN, SCAN,
            ),
        )
        conn.commit()
        conn.close()

        database.initialize_database()
        with database.connection() as conn:
            row = conn.execute("SELECT * FROM projects WHERE source_key='legacy'").fetchone()
        self.assertEqual("Legacy Title", row["title"])
        self.assertEqual(SCAN, row["scan_at"])
        self.assertEqual(SCAN, row["posted_at"])
        self.assertEqual("Start: ASAP | Duration: 3 months", row["project_length"])
        self.assertEqual("€500/day", row["budget"])
        self.assertEqual("Freelance", row["engagement_type"])


if __name__ == "__main__":
    unittest.main()