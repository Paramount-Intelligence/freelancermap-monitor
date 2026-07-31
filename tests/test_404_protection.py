from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import database
from browser import PageNotFoundError, ERROR_BODY_RE, ERROR_TITLE_RE
from config import Config
from monitor import DetailValidationError, validate_detail
from parser import parse_project_detail


HTML_404 = """<!DOCTYPE html>
<html>
<head><title>404 Not Found - Freelancermap</title></head>
<body>
<main>
    <h1>404 - Page Not Found</h1>
    <p>The requested project does not exist.</p>
</main>
</body>
</html>"""

HTML_500 = """<!DOCTYPE html>
<html>
<head><title>500 Internal Server Error</title></head>
<body>
<main>
    <h1>500 - Server Error</h1>
    <p>Something went wrong on our end.</p>
</main>
</body>
</html>"""


class Test404Protection(unittest.TestCase):
    def test_browser_error_title_regex_matches_404_and_errors(self):
        self.assertTrue(ERROR_TITLE_RE.search("404 Not Found"))
        self.assertTrue(ERROR_TITLE_RE.search("Page Not Found"))
        self.assertTrue(ERROR_TITLE_RE.search("500 Internal Server Error"))
        self.assertTrue(ERROR_TITLE_RE.search("410 Resource is Gone"))
        self.assertTrue(ERROR_TITLE_RE.search("429 Too Many Requests"))
        self.assertFalse(ERROR_TITLE_RE.search("Senior Python Developer"))

    def test_error_body_regex_does_not_flag_currency_amounts(self):
        self.assertFalse(ERROR_BODY_RE.search("SC Cleared SOC Analyst - £500/day via Umbrella"))
        self.assertFalse(ERROR_BODY_RE.search("€404,000 budget per year"))
        self.assertFalse(ERROR_BODY_RE.search("Rate: $500.00 per day"))
        self.assertFalse(ERROR_BODY_RE.search("Onboarding fee 1.500 EUR"))
        self.assertFalse(ERROR_BODY_RE.search("Senior Python Developer - £410/day contract"))

    def test_error_body_regex_still_flags_real_error_states(self):
        self.assertTrue(ERROR_BODY_RE.search("500 Internal Server Error"))
        self.assertTrue(ERROR_BODY_RE.search("Page 404 - Not Found"))
        self.assertTrue(ERROR_BODY_RE.search("HTTP 429 Too Many Requests"))
        self.assertTrue(ERROR_BODY_RE.search("Something went wrong, please try again later"))

    def test_parser_rejects_404_page(self):
        detail = parse_project_detail(HTML_404, "https://www.freelancermap.com/project/test", "https://www.freelancermap.com")
        self.assertEqual("", detail.title)
        self.assertEqual("", detail.description)

    def test_monitor_validate_detail_rejects_404_title(self):
        detail = parse_project_detail(HTML_404, "https://www.freelancermap.com/project/test", "https://www.freelancermap.com")
        with self.assertRaises(DetailValidationError):
            validate_detail(detail)

    def test_database_v8_migration_repairs_corrupted_404_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test_v8.db"
            with patch.object(Config, "DATABASE_PATH", db), patch.object(database, "DATABASE_PATH", db):
                database.initialize_database()
                
                # Insert a simulated corrupted row with title '404 Not Found'
                with database.connection(write=True) as conn:
                    conn.execute(
                        """
                        INSERT INTO projects (source_key, slug, url, title, title_hint, card_location, location, scan_at, posted_at, first_seen_at, last_seen_at, created_at, updated_at, detail_fetch_status)
                        VALUES ('corrupted-key', 'corrupted-slug', 'https://www.freelancermap.com/project/corrupted', '404 Not Found', 'Original Real Title', 'Berlin, Germany', '404 Not Found', '2026-07-31T00:00:00+00:00', '2026-07-31T00:00:00+00:00', '2026-07-31T00:00:00+00:00', '2026-07-31T00:00:00+00:00', '2026-07-31T00:00:00+00:00', '2026-07-31T00:00:00+00:00', 'success')
                        """
                    )
                
                # Run v8 repair schema
                with database.connection(write=True) as conn:
                    database._repair_schema_v8(conn)
                
                # Verify row is repaired back to original hint and marked failed
                with database.connection() as conn:
                    row = conn.execute("SELECT title, location, detail_fetch_status FROM projects WHERE source_key = 'corrupted-key'").fetchone()
                    self.assertEqual("Original Real Title", row["title"])
                    self.assertEqual("Berlin, Germany", row["location"])
                    self.assertEqual("failed", row["detail_fetch_status"])


if __name__ == "__main__":
    unittest.main()
