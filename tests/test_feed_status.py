from __future__ import annotations

import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

import database
import monitor
from config import Config
from parser import ProjectDiscovery


BASE = "https://www.freelancermap.com"
SCAN = "2026-07-31T00:00:00+00:00"


class SchemaV10FeedStatusTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db_path = Path(self.tempdir.name) / "v10.db"

    def test_scans_table_has_feed_status_columns(self):
        with patch.object(Config, "DATABASE_PATH", self.db_path), \
             patch.object(database, "DATABASE_PATH", self.db_path):
            database.initialize_database()
            with database.connection() as conn:
                columns = {
                    str(row["name"])
                    for row in conn.execute("PRAGMA table_info(scans)").fetchall()
                }
                version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        self.assertEqual(database.SCHEMA_VERSION, version)
        for column in (
            "primary_feed_status",
            "personalized_feed_status",
            "degraded",
            "degraded_reason",
            "primary_count",
            "personalized_count",
            "personalized_only_count",
            "ignored_personalized_only_count",
        ):
            self.assertIn(column, columns)

    def test_v9_database_upgrades_without_losing_scan_rows(self):
        with patch.object(Config, "DATABASE_PATH", self.db_path), \
             patch.object(database, "DATABASE_PATH", self.db_path):
            database.initialize_database()
            with database.connection(write=True) as conn:
                conn.execute("PRAGMA user_version = 9")
                conn.execute(
                    "INSERT INTO scans(started_at, status) VALUES ('2026-07-30T00:00:00+00:00', 'success')"
                )
            database.initialize_database()
            with database.connection() as conn:
                row = conn.execute("SELECT * FROM scans LIMIT 1").fetchone()
        self.assertEqual("success", row["status"])
        self.assertEqual("", row["primary_feed_status"])
        self.assertEqual(0, row["degraded"])

    def test_finish_scan_persists_feed_status(self):
        with patch.object(Config, "DATABASE_PATH", self.db_path), \
             patch.object(database, "DATABASE_PATH", self.db_path):
            database.initialize_database()
            scan_id = database.create_scan()
            database.finish_scan(
                scan_id,
                status="success",
                discovered_count=3,
                primary_feed_status="ok",
                personalized_feed_status="failed",
                degraded=True,
                degraded_reason="personalized feed timed out",
                primary_count=3,
                personalized_count=0,
            )
            with database.connection() as conn:
                row = conn.execute("SELECT * FROM scans WHERE id = ?", (scan_id,)).fetchone()
        self.assertEqual("ok", row["primary_feed_status"])
        self.assertEqual("failed", row["personalized_feed_status"])
        self.assertEqual(1, row["degraded"])
        self.assertEqual("personalized feed timed out", row["degraded_reason"])
        self.assertEqual(3, row["primary_count"])


class BaselineResetTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db_path = Path(self.tempdir.name) / "reset.db"
        self.patches = (
            patch.object(Config, "DATABASE_PATH", self.db_path),
            patch.object(database, "DATABASE_PATH", self.db_path),
        )
        for item in self.patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in reversed(self.patches)])
        database.initialize_database()
        database.set_setting("baseline_initialized", "true")
        database.set_setting("baseline_initializing", "false")
        database.set_setting("baseline_started_at", SCAN)
        database.set_setting("baseline_completed_at", SCAN)

    def discovery(self, slug):
        return ProjectDiscovery(
            source_key=slug,
            slug=slug,
            url=f"{BASE}/project/{slug}",
            title_hint="Project",
        )

    def test_reset_baseline_re_marks_rows_and_clears_settings(self):
        database.upsert_discovery(self.discovery("pending"), baseline=False)
        database.upsert_discovery(self.discovery("sent"), baseline=False)
        with database.connection(write=True) as conn:
            conn.execute("UPDATE projects SET email_status='sent' WHERE slug='sent'")
        changed = database.reset_baseline()
        self.assertEqual(1, changed)
        with database.connection() as conn:
            pending = conn.execute("SELECT email_status, baseline FROM projects WHERE slug='pending'").fetchone()
            sent = conn.execute("SELECT email_status, baseline FROM projects WHERE slug='sent'").fetchone()
        self.assertEqual("baseline", pending["email_status"])
        self.assertEqual(1, pending["baseline"])
        self.assertEqual("sent", sent["email_status"])
        self.assertEqual(0, sent["baseline"])
        self.assertFalse(database.baseline_initialized())
        self.assertFalse(database.get_setting("baseline_initializing", "true") == "true")
        self.assertEqual("", database.get_setting("baseline_started_at"))
        self.assertEqual("", database.get_setting("baseline_completed_at"))

    def test_reset_baseline_suppresses_pending_email_batches(self):
        database.upsert_discovery(self.discovery("old"), baseline=False)
        database.reset_baseline()
        self.assertEqual([], database.pending_email_projects())


class CycleFeedStatusTests(unittest.TestCase):
    def discovery(self, slug="example"):
        return ProjectDiscovery(
            source_key=slug,
            slug=slug,
            url=f"{BASE}/project/{slug}",
            title_hint="Example project",
            scan_at=SCAN,
        )

    def common_patches(self):
        return [
            patch("monitor.exclusive_file_lock", return_value=nullcontext()),
            patch("monitor._validate_runtime_configuration"),
            patch("monitor.database.initialize_database"),
            patch("monitor._reconcile_accepted_email_receipts"),
            patch("monitor.database.create_scan", return_value=11),
            patch("monitor.database.finish_scan"),
            patch("monitor.BrowserSession", MonitorTestFakeBrowser),
            patch("monitor._require_authenticated_session"),
            patch("monitor._projects_needing_details", return_value=[]),
            patch("monitor._record_empty_scan"),
            patch("monitor._reset_empty_scan_counter"),
        ]

    def test_successful_cycle_records_primary_ok(self):
        patches = self.common_patches()
        for item in patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in reversed(patches)])
        outcome = monitor.DiscoveryOutcome(
            projects=[self.discovery()],
            primary_count=1,
            personalized_feed_status="not_configured",
        )
        with patch("monitor._baseline_initialized", return_value=True), patch(
            "monitor._setting_bool", return_value=False
        ), patch("monitor._set_setting"), patch(
            "monitor._discover", return_value=outcome
        ), patch(
            "monitor.database.upsert_discovery", return_value=(1, True)
        ), patch("monitor._send_one_pending_email_batch"), patch.object(
            Config, "MAX_PROJECTS_PER_CYCLE", 100
        ):
            result = monitor.run_cycle()
        self.assertEqual("ok", result.primary_feed_status)
        self.assertEqual("not_configured", result.personalized_feed_status)
        finish_call = monitor.database.finish_scan.call_args
        self.assertEqual("ok", finish_call.kwargs["primary_feed_status"])
        self.assertEqual("not_configured", finish_call.kwargs["personalized_feed_status"])
        self.assertFalse(finish_call.kwargs["degraded"])
        self.assertEqual(1, finish_call.kwargs["primary_count"])

    def test_degraded_cycle_still_succeeds_with_personalized_failure(self):
        patches = self.common_patches()
        for item in patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in reversed(patches)])
        outcome = monitor.DiscoveryOutcome(
            projects=[self.discovery()],
            personalized_feed_status="failed",
            degraded=True,
            degraded_reason="boom",
        )
        with patch("monitor._baseline_initialized", return_value=True), patch(
            "monitor._setting_bool", return_value=False
        ), patch("monitor._set_setting"), patch(
            "monitor._discover", return_value=outcome
        ), patch(
            "monitor.database.upsert_discovery", return_value=(1, True)
        ), patch("monitor._send_one_pending_email_batch"), patch.object(
            Config, "MAX_PROJECTS_PER_CYCLE", 100
        ):
            result = monitor.run_cycle()
        self.assertTrue(result.degraded)
        self.assertEqual("failed", result.personalized_feed_status)
        self.assertEqual("ok", result.primary_feed_status)

    def test_required_personalized_feed_failure_fails_cycle(self):
        patches = self.common_patches()
        for item in patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in reversed(patches)])
        outcome = monitor.DiscoveryOutcome(
            projects=[self.discovery()],
            personalized_feed_status="failed",
            degraded=True,
            degraded_reason="boom",
        )
        error = monitor.PersonalizedFeedError(
            "PERSONALIZED_FEED_REQUIRED=true: personalized feed failed", outcome
        )
        with patch("monitor._baseline_initialized", return_value=True), patch(
            "monitor._setting_bool", return_value=False
        ), patch("monitor._set_setting"), patch(
            "monitor._discover", side_effect=error
        ), patch.object(Config, "MAX_PROJECTS_PER_CYCLE", 100):
            with self.assertRaisesRegex(RuntimeError, "PERSONALIZED_FEED_REQUIRED=true"):
                monitor.run_cycle()
        finish_call = monitor.database.finish_scan.call_args
        self.assertEqual("failed", finish_call.kwargs["status"])
        self.assertEqual("failed", finish_call.kwargs["personalized_feed_status"])
        self.assertEqual("ok", finish_call.kwargs["primary_feed_status"])
        self.assertTrue(finish_call.kwargs["degraded"])


class MonitorTestFakeBrowser:
    def __init__(self, headless=None):
        self.headless = headless
        self.driver = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def is_logged_in(self):
        return True


if __name__ == "__main__":
    unittest.main()
