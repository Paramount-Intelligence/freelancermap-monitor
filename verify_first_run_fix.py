from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import database
from browser import AuthVerificationResult
from config import Config
from monitor import run_cycle


HTML_FIRST_RUN = """<main>
<div class="project-card">
    <a data-testid="title" href="/project/first-run-project-1">First Run Project One</a>
    <div data-testid="city">Munich, Germany</div>
    <div data-testid="type">Freelance</div>
    <div data-testid="duration">6 months</div>
</div>
<div class="project-card">
    <a data-testid="title" href="/project/first-run-project-2">First Run Project Two</a>
    <div data-testid="city">Berlin, Germany</div>
    <div data-testid="type">Contract</div>
    <div data-testid="duration">12 months</div>
</div>
</main>"""

DETAIL_PROJECT_1 = """<main>
<div class="project-header"><h1>First Run Project One</h1></div>
<div class="project-body-description"><p>Full detailed description for first run project 1 containing ample prose.</p></div>
</main>"""

DETAIL_PROJECT_2 = """<main>
<div class="project-header"><h1>First Run Project Two</h1></div>
<div class="project-body-description"><p>Full detailed description for first run project 2 containing ample prose.</p></div>
</main>"""


class VerificationBrowser:
    def __init__(self, html_listing, detail_map):
        self.html_listing = html_listing
        self.detail_map = detail_map
        self.driver = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def load_listing_page(self, url, *, expected_sort=None):
        return self.html_listing

    def load_detail_page(self, url):
        return self.get(url)

    def get(self, url):
        for key, html in self.detail_map.items():
            if key in url:
                return html
        return "<main><h1>Fallback Detail</h1><div class=\"project-body-description\"><p>Detailed fallback description text for test validation.</p></div></main>"

    def is_logged_in(self):
        return True

    def verify_authenticated_session(self):
        return AuthVerificationResult(authenticated=True, reason="Mock session authenticated")


def verify_fix():
    print("=== VERIFYING FIRST-RUN IMMEDIATE EMAIL FIX ===")
    
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        db_path = tmp_path / "verify_first_run.db"
        lock_path = tmp_path / "verify_first_run.lock"
        
        # Patch Config and database paths with AUTO_BASELINE_ON_FIRST_RUN = False (new default)
        with patch.object(Config, "DATABASE_PATH", db_path), \
             patch.object(database, "DATABASE_PATH", db_path), \
             patch.object(Config, "LOCK_PATH", lock_path), \
             patch.object(Config, "PRIMARY_SEARCH_URL", "https://www.freelancermap.com/projects?sort=1"), \
             patch.object(Config, "AUTO_BASELINE_ON_FIRST_RUN", False):
            
            # Step 1: Initialize empty database
            database.initialize_database()
            print("1. Database initialized (fresh empty database).")
            assert database.database_is_empty() is True

            # Mock BrowserSession and Mock SMTP Send
            mock_browser = lambda headless=None: VerificationBrowser(
                HTML_FIRST_RUN,
                {"first-run-project-1": DETAIL_PROJECT_1, "first-run-project-2": DETAIL_PROJECT_2}
            )

            sent_digests = []

            def mock_send(message, recipients=None):
                sent_digests.append(message)
                return "<first-run-batch-id@freelancermap>"

            # Step 2: First run on a fresh database MUST be refused (safety gate)
            print("\n2. Executing First Run cycle on fresh database...")
            with patch("monitor.BrowserSession", mock_browser), \
                 patch("emailer._send", side_effect=mock_send):
                try:
                    run_cycle()
                except RuntimeError as exc:
                    print(f"   First Run refused with RuntimeError: {exc}")
                    assert "Baseline is not initialized" in str(exc)
                else:
                    raise AssertionError(
                        "First run must refuse to scan/email when the baseline "
                        "is not initialized and AUTO_BASELINE_ON_FIRST_RUN=false."
                    )

                assert len(sent_digests) == 0, "No email may be sent by a refused run!"
                print("3. Verification successful: no projects scanned and 0 emails sent.")

            # Step 3: Explicit baseline initialization is required first
            print("\n4. Running explicit baseline initialization...")
            with patch("monitor.BrowserSession", mock_browser), \
                 patch("emailer._send", side_effect=mock_send):
                res = run_cycle(force_baseline=True)
                assert res.baseline is True
                assert res.discovered == 2
                assert res.new == 2
                assert res.emailed == 0
                assert len(sent_digests) == 0
                print("5. Baseline initialized with 2 projects; 0 emails sent.")

            # Step 4: Inspect database rows to confirm email_status = 'baseline'
            print("\n6. Database Row Inspection:")
            with database.connection() as conn:
                projects = conn.execute("SELECT id, title, email_status, emailed_at FROM projects ORDER BY id").fetchall()
                for p in projects:
                    print(f"   [ID {p['id']}] Title: '{p['title']}' | Email Status: '{p['email_status']}' | Emailed At: '{p['emailed_at']}'")
                    assert p["email_status"] == "baseline", f"Expected email_status 'baseline', got '{p['email_status']}'"
                    assert p["emailed_at"] is None, "Baseline rows must never be emailed!"

    print("\nFIRST-RUN BASELINE SAFETY GATE IS EMPIRICALLY VERIFIED & WORKING!")


if __name__ == "__main__":
    verify_fix()
