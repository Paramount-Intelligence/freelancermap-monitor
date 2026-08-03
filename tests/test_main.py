from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main
from config import Config
from monitor import CycleResult


class HeartbeatFeedStatusTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "heartbeat.json"

    def read_payload(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_heartbeat_cycle_includes_feed_status(self):
        result = CycleResult(
            discovered=3,
            new=2,
            baseline=False,
            primary_feed_status="ok",
            personalized_feed_status="failed",
            degraded=True,
            degraded_reason="personalized feed timed out",
        )
        with patch.object(Config, "HEARTBEAT_ENABLED", True), \
             patch.object(Config, "HEARTBEAT_PATH", self.path):
            main.write_heartbeat("success", "once", result=result)
        cycle = self.read_payload()["cycle"]
        self.assertEqual("ok", cycle["primary_feed_status"])
        self.assertEqual("failed", cycle["personalized_feed_status"])
        self.assertTrue(cycle["degraded"])
        self.assertEqual("personalized feed timed out", cycle["degraded_reason"])

    def test_heartbeat_without_result_has_no_cycle(self):
        with patch.object(Config, "HEARTBEAT_ENABLED", True), \
             patch.object(Config, "HEARTBEAT_PATH", self.path):
            main.write_heartbeat("running", "once")
        self.assertNotIn("cycle", self.read_payload())

    def test_heartbeat_disabled_writes_nothing(self):
        with patch.object(Config, "HEARTBEAT_ENABLED", False), \
             patch.object(Config, "HEARTBEAT_PATH", self.path):
            main.write_heartbeat("success", "once")
        self.assertFalse(self.path.exists())


class PrimaryFeedUrlTests(unittest.TestCase):
    def test_primary_feed_url_uses_primary_search_url(self):
        with patch.object(Config, "PRIMARY_SEARCH_URL", "https://www.freelancermap.com/projects?q=x"), \
             patch.object(Config, "PROJECTS_URL", "https://www.freelancermap.com/projects"), \
             patch.object(Config, "FEED_QUERY_SORT_PARAM", "sort"), \
             patch.object(Config, "PRIMARY_FEED_NEWEST_SORT_VALUE", "1"):
            self.assertEqual(
                "https://www.freelancermap.com/projects?q=x&sort=1",
                main.primary_feed_url(),
            )

    def test_primary_feed_url_appends_sort_when_missing(self):
        with patch.object(Config, "PRIMARY_SEARCH_URL", ""), \
             patch.object(Config, "PROJECTS_URL", "https://www.freelancermap.com/projects"), \
             patch.object(Config, "FEED_QUERY_SORT_PARAM", "sort"), \
             patch.object(Config, "PRIMARY_FEED_NEWEST_SORT_VALUE", "1"):
            self.assertEqual(
                "https://www.freelancermap.com/projects?sort=1",
                main.primary_feed_url(),
            )

    def test_primary_feed_url_never_duplicates_sort_param(self):
        with patch.object(Config, "PRIMARY_SEARCH_URL", "https://www.freelancermap.com/projects?sort=1"), \
             patch.object(Config, "PROJECTS_URL", "https://www.freelancermap.com/projects"), \
             patch.object(Config, "FEED_QUERY_SORT_PARAM", "sort"), \
             patch.object(Config, "PRIMARY_FEED_NEWEST_SORT_VALUE", "1"):
            url = main.primary_feed_url()
            self.assertEqual(1, url.count("sort="))


class ResultLineFeedStatusTests(unittest.TestCase):
    def test_result_line_reports_feed_statuses(self):
        result = CycleResult(
            primary_feed_status="ok",
            personalized_feed_status="not_configured",
            degraded=False,
        )
        line = main.result_line(result)
        self.assertIn("primary_feed=ok", line)
        self.assertIn("personalized_feed=not_configured", line)
        self.assertIn("degraded=False", line)


if __name__ == "__main__":
    unittest.main()
