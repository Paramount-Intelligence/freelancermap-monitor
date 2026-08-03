from __future__ import annotations

import math
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from utils import (
    URLNormalizationError,
    canonicalize_url,
    exclusive_file_lock,
    json_dumps,
    local_now_display,
    normalize_space,
    polite_sleep,
    source_key_from_url,
    stable_hash,
    utc_now_iso,
)


class UtilsEnhancedTests(unittest.TestCase):
    def test_utc_now_is_timezone_aware_and_second_precision(self):
        value = utc_now_iso()
        parsed = datetime.fromisoformat(value)
        self.assertEqual(timezone.utc, parsed.tzinfo)
        self.assertEqual(0, parsed.microsecond)

    def test_local_display_uses_requested_zone(self):
        instant = datetime(2026, 7, 30, 22, 0, tzinfo=timezone.utc)
        value = local_now_display("Asia/Karachi", now=instant)
        self.assertIn("31 Jul 2026", value)
        self.assertIn("03:00 AM", value)
        self.assertIn("PKT", value)

    def test_normalize_space_handles_unicode_and_invisible_marks(self):
        self.assertEqual(
            "Alpha Beta Gamma",
            normalize_space("  Alpha\u00a0\nBeta\u200b\tGamma  "),
        )

    def test_canonicalize_url_normalizes_safe_identity_components(self):
        value = canonicalize_url(
            "/project/Example%2fSlug/?utm_source=x#details",
            "HTTPS://WWW.FreelancerMap.COM:443",
        )
        self.assertEqual(
            "https://www.freelancermap.com/project/Example%2FSlug",
            value,
        )

    def test_canonicalize_url_keeps_non_default_port(self):
        self.assertEqual(
            "https://example.com:8443/project/x",
            canonicalize_url("/project/x", "https://EXAMPLE.com:8443"),
        )

    def test_canonicalize_url_rejects_unsafe_or_ambiguous_values(self):
        cases = [
            ("javascript:alert(1)", "https://example.com"),
            ("https://user:pass@example.com/project/x", "https://example.com"),
            ("https://example.com\\@evil.test/project/x", "https://example.com"),
            ("https://example.com/project/x\nHost: evil.test", "https://example.com"),
        ]
        for url, base in cases:
            with self.subTest(url=url):
                with self.assertRaises(URLNormalizationError):
                    canonicalize_url(url, base)

    def test_source_key_uses_decoded_final_segment_and_hash_fallback(self):
        self.assertEqual("hello-world", source_key_from_url("https://example.com/project/hello-world/"))
        self.assertEqual("hello world", source_key_from_url("https://example.com/project/hello%20world"))
        fallback = source_key_from_url("https://example.com/")
        self.assertEqual(64, len(fallback))
        int(fallback, 16)

    def test_stable_hash_is_deterministic_for_dicts_sets_and_datetimes(self):
        first = {
            "b": {3, 1, 2},
            "a": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }
        second = {
            "a": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "b": {2, 3, 1},
        }
        self.assertEqual(stable_hash(first), stable_hash(second))

    def test_json_dumps_is_compact_sorted_and_rejects_nan(self):
        self.assertEqual('{"a":1,"b":2}', json_dumps({"b": 2, "a": 1}))
        with self.assertRaises(ValueError):
            json_dumps({"bad": math.nan})

    def test_polite_sleep_sorts_and_clamps_bounds(self):
        with (
            patch("utils._JITTER_RANDOM.uniform", return_value=1.25) as uniform,
            patch("utils.time.sleep") as sleep,
        ):
            polite_sleep(2.0, 1.0)
            uniform.assert_called_once_with(1.0, 2.0)
            sleep.assert_called_once_with(1.25)

        with patch("utils.time.sleep") as sleep:
            polite_sleep(-4, -1)
            sleep.assert_called_once_with(0.0)

    def test_polite_sleep_rejects_non_finite_values(self):
        for value in (math.inf, -math.inf, math.nan):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    polite_sleep(value, 1)

    def test_exclusive_file_lock_rejects_concurrent_holder_and_keeps_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitor.lock"
            with exclusive_file_lock(path):
                self.assertTrue(path.exists())
                with self.assertRaises(RuntimeError):
                    with exclusive_file_lock(path):
                        pass
            self.assertTrue(path.exists())
            metadata = path.read_text(encoding="utf-8")
            self.assertIn('"pid":', metadata)

    def test_exclusive_file_lock_validates_wait_parameters(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitor.lock"
            with self.assertRaises(ValueError):
                with exclusive_file_lock(path, timeout_seconds=-1):
                    pass
            with self.assertRaises(ValueError):
                with exclusive_file_lock(path, timeout_seconds=1, poll_interval_seconds=0):
                    pass


if __name__ == "__main__":
    unittest.main()