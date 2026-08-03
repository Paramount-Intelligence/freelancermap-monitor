from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

import database
import monitor
from config import Config
from parser import ProjectDetail, ProjectDiscovery


BASE = "https://www.freelancermap.com"
SCAN = "2026-07-31T00:00:00+00:00"


class FakeBrowser:
    def __init__(self, headless=None):
        self.headless = headless
        self.driver = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def is_logged_in(self):
        return True


class MonitorEnhancedTests(unittest.TestCase):
    def discovery(self, slug="example"):
        return ProjectDiscovery(
            source_key=slug,
            slug=slug,
            url=f"{BASE}/project/{slug}",
            title_hint="Example project",
            card_description="Useful project description.",
            scan_at=SCAN,
        ).finalize()

    def detail(self, slug="example"):
        return ProjectDetail(
            source_key=slug,
            slug=slug,
            url=f"{BASE}/project/{slug}",
            title="Example project",
            description="Complete project description.",
            scan_at=SCAN,
        ).finalize()

    def common_run_patches(self):
        return [
            patch("monitor.exclusive_file_lock", return_value=nullcontext()),
            patch("monitor._validate_runtime_configuration"),
            patch("monitor.database.initialize_database"),
            patch("monitor._reconcile_accepted_email_receipts"),
            patch("monitor.database.create_scan", return_value=11),
            patch("monitor.database.finish_scan"),
            patch("monitor.BrowserSession", FakeBrowser),
            patch("monitor._require_authenticated_session"),
            patch("monitor._projects_needing_details", return_value=[]),
            patch("monitor._record_empty_scan"),
            patch("monitor._reset_empty_scan_counter"),
        ]

    def test_first_successful_scan_is_baseline_and_does_not_email(self):
        patches = self.common_run_patches()
        started = [item.start() for item in patches]
        self.addCleanup(lambda: [item.stop() for item in reversed(patches)])

        with patch("monitor._baseline_initialized", return_value=False), patch(
            "monitor._setting_bool", return_value=False
        ), patch("monitor._set_setting") as setting, patch(
            "monitor._discover",
            return_value=monitor.DiscoveryOutcome(projects=[self.discovery()]),
        ), patch(
            "monitor.database.upsert_discovery", return_value=(1, True)
        ), patch(
            "monitor._mark_baseline_projects", return_value=1
        ), patch(
            "monitor._send_one_pending_email_batch"
        ) as sender, patch.object(
            Config, "AUTO_BASELINE_ON_FIRST_RUN", True
        ), patch.object(
            Config, "MAX_PROJECTS_PER_CYCLE", 100
        ):
            result = monitor.run_cycle()

        self.assertTrue(result.baseline)
        self.assertEqual(1, result.discovered)
        self.assertEqual(1, result.new)
        sender.assert_not_called()
        setting.assert_any_call("baseline_initializing", "true")
        setting.assert_any_call("baseline_initialized", "true")
        setting.assert_any_call("baseline_initializing", "false")

    def test_empty_first_scan_cannot_complete_baseline(self):
        patches = self.common_run_patches()
        [item.start() for item in patches]
        self.addCleanup(lambda: [item.stop() for item in reversed(patches)])

        with patch("monitor._baseline_initialized", return_value=False), patch(
            "monitor._setting_bool", return_value=False
        ), patch("monitor._set_setting"), patch(
            "monitor._discover",
            return_value=monitor.DiscoveryOutcome(projects=[]),
        ), patch.object(
            Config, "AUTO_BASELINE_ON_FIRST_RUN", True
        ), patch.object(
            Config, "ALLOW_EMPTY_RESULTS", True
        ):
            with self.assertRaisesRegex(monitor.DiscoveryError, "Baseline initialization was refused"):
                monitor.run_cycle()

    def test_project_limit_is_never_silently_sliced(self):
        patches = self.common_run_patches()
        [item.start() for item in patches]
        self.addCleanup(lambda: [item.stop() for item in reversed(patches)])

        with patch("monitor._baseline_initialized", return_value=True), patch(
            "monitor._setting_bool", return_value=False
        ), patch(
            "monitor._discover",
            return_value=monitor.DiscoveryOutcome(
                projects=[self.discovery("one"), self.discovery("two")]
            ),
        ), patch(
            "monitor.database.upsert_discovery"
        ) as upsert, patch.object(
            Config, "AUTO_BASELINE_ON_FIRST_RUN", True
        ), patch.object(
            Config, "MAX_PROJECTS_PER_CYCLE", 1
        ):
            with self.assertRaisesRegex(monitor.DiscoveryError, "refuses to silently discard"):
                monitor.run_cycle()
        upsert.assert_not_called()

    def test_listing_retry_recovers_after_transient_empty_parse(self):
        browser = Mock()
        browser.load_listing_page.return_value = "<html></html>"
        item = self.discovery()
        with patch("monitor._parse_listing", side_effect=[[], [item]]), patch(
            "monitor._browser_project_route_count", return_value=0
        ), patch("monitor.time.sleep"), patch.object(
            Config, "EMPTY_RESULT_RETRIES", 1
        ), patch.object(
            Config, "EMPTY_RESULT_RETRY_SECONDS", 0
        ):
            result = monitor._load_listing_with_retries(
                browser,
                BASE + "/my_account.html",
                scan_at=SCAN,
                page_number=1,
            )
        self.assertEqual([item], result)
        self.assertEqual(2, browser.load_listing_page.call_count)

    def test_parser_coverage_guard_rejects_large_silent_loss(self):
        with patch.object(Config, "MIN_PARSER_COVERAGE_RATIO", 0.70, create=True), patch.object(
            Config, "MIN_PARSER_COVERAGE_GAP", 3, create=True
        ):
            with self.assertRaisesRegex(monitor.DiscoveryError, "Parser coverage is suspicious"):
                monitor._validate_parser_coverage(
                    parsed_count=2,
                    dom_route_count=10,
                    page_number=1,
                )

    def test_dead_browser_aborts_without_project_failure_classification(self):
        browser = Mock()
        browser.get_project_page.side_effect = RuntimeError("invalid session id")
        with patch.object(Config, "DETAIL_PAGE_RETRIES", 0, create=True), patch(
            "monitor._capture_diagnostic"
        ):
            with self.assertRaises(monitor.BrowserSessionLostError):
                monitor._fetch_and_parse_detail(
                    browser,
                    {"id": 1, "url": BASE + "/project/example"},
                    scan_at=SCAN,
                )

    def test_detail_canonical_must_match_requested_project(self):
        detail = self.detail("different")
        with self.assertRaisesRegex(monitor.DetailValidationError, "does not match"):
            monitor.validate_detail(
                detail,
                expected_url=BASE + "/project/example",
            )

    def test_smtp_success_is_receipted_before_marking_sent(self):
        row = {"id": 7}
        receipt = Path(tempfile.gettempdir()) / "accepted-test.json"
        receipt.unlink(missing_ok=True)
        order = []

        def write_receipt(**kwargs):
            order.append("receipt")
            receipt.write_text("{}", encoding="utf-8")
            return receipt

        def mark_sent(ids, message_id):
            order.append("mark")

        with patch("monitor._pending_email_projects", return_value=[row]), patch(
            "monitor._ensure_email_receipt_directory"
        ), patch("monitor.send_projects_email", return_value="<batch@example.com>"), patch(
            "monitor._write_accepted_email_receipt", side_effect=write_receipt
        ), patch("monitor._mark_projects_emailed", side_effect=mark_sent), patch.object(
            monitor.database, "start_email_batch", Mock(), create=True
        ):
            sent = monitor._send_one_pending_email_batch()

        self.assertEqual(1, sent)
        self.assertEqual(["receipt", "mark"], order)
        self.assertFalse(receipt.exists())

    def test_smtp_failure_leaves_rows_pending_and_records_failure(self):
        with patch("monitor._pending_email_projects", return_value=[{"id": 9}]), patch(
            "monitor._ensure_email_receipt_directory"
        ), patch("monitor.send_projects_email", side_effect=RuntimeError("SMTP down")), patch(
            "monitor._mark_email_failure"
        ) as mark_failure, patch("monitor._mark_projects_emailed") as mark_sent:
            with self.assertRaisesRegex(RuntimeError, "SMTP down"):
                monitor._send_one_pending_email_batch()
        mark_failure.assert_called_once()
        mark_sent.assert_not_called()

    def test_accepted_receipt_is_reconciled_before_future_sends(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "smtp_receipts"
            directory.mkdir()
            receipt = directory / "accepted-batch.json"
            receipt.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "accepted_at": SCAN,
                        "message_id": "<batch@example.com>",
                        "project_ids": [4, 5],
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(Config, "DATA_DIR", Path(tmp)), patch(
                "monitor._mark_projects_emailed"
            ) as mark_sent:
                monitor._reconcile_accepted_email_receipts()
            mark_sent.assert_called_once_with([4, 5], "<batch@example.com>")
            self.assertFalse(receipt.exists())

    def test_safe_same_origin_url_preserves_query_string_and_fragment(self):
        url = (
            "https://www.freelancermap.com/projects"
            "?excludeDachProjects=false&query=website+development&sort=1&pagenr=1"
        )
        self.assertEqual(
            url,
            monitor._safe_same_origin_url(url),
        )
        self.assertEqual(
            "https://www.freelancermap.com/projects?sort=1#list",
            monitor._safe_same_origin_url("https://www.freelancermap.com/projects?sort=1#list"),
        )

    def test_safe_same_origin_url_rejects_cross_origin(self):
        with self.assertRaisesRegex(monitor.DiscoveryError, "Cross-origin"):
            monitor._safe_same_origin_url("https://evil.example.com/projects")

    def test_safe_same_origin_url_joins_relative_paths(self):
        self.assertEqual(
            "https://www.freelancermap.com/projects?sort=1",
            monitor._safe_same_origin_url("/projects?sort=1"),
        )

    def test_refused_first_run_leaves_zero_scan_rows_and_no_baseline_state(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "test_refused.db"
            lock_path = Path(folder) / "test_refused.lock"
            with patch.object(database, "DATABASE_PATH", db_path), \
                 patch.object(Config, "LOCK_PATH", lock_path), \
                 patch.object(Config, "AUTO_BASELINE_ON_FIRST_RUN", False), \
                 patch.object(Config, "PRIMARY_SEARCH_URL", "https://www.freelancermap.com/projects?sort=1"), \
                 patch("monitor.exclusive_file_lock"):
                database.initialize_database()
                with self.assertRaisesRegex(RuntimeError, "Baseline is not initialized"):
                    monitor.run_cycle(dry_run=False, force_baseline=False, headless=True)
                with database.connection() as conn:
                    total = conn.execute(
                        "SELECT COUNT(*) AS c FROM scans"
                    ).fetchone()["c"]
                    running = conn.execute(
                        "SELECT COUNT(*) AS c FROM scans WHERE status='running'"
                    ).fetchone()["c"]
                self.assertEqual(0, total)
                self.assertEqual(0, running)
                self.assertEqual(
                    "false",
                    database.get_setting("baseline_initializing", "false"),
                )
                self.assertEqual(
                    "false",
                    database.get_setting("baseline_started_at", "false"),
                )


if __name__ == "__main__":
    unittest.main()