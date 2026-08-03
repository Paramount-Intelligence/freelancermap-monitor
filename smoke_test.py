from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import database
from browser import AuthVerificationResult
from config import Config
from monitor import run_cycle


HTML_CYCLE_1 = """<main>
<div class="project-card">
    <a data-testid="title" href="/project/baseline-1">Baseline Project One</a>
    <div data-testid="city">Berlin, Germany</div>
    <div data-testid="type">Freelance</div>
    <div data-testid="duration">6 months</div>
</div>
<div class="project-card">
    <a data-testid="title" href="/project/baseline-2">Baseline Project Two</a>
    <div data-testid="city">Amsterdam, Netherlands</div>
    <div data-testid="type">Contract</div>
    <div data-testid="duration">12 months</div>
</div>
</main>"""

HTML_CYCLE_2 = """<main>
<div class="project-card">
    <a data-testid="title" href="/project/baseline-1">Baseline Project One</a>
</div>
<div class="project-card">
    <a data-testid="title" href="/project/baseline-2">Baseline Project Two</a>
</div>
<div class="project-card">
    <a data-testid="title" href="/project/new-hero-project">New Hero Project</a>
    <div data-testid="city">Rozenburg, Netherlands</div>
    <div data-testid="type">Freelance</div>
    <div data-testid="duration">6 months initial contract</div>
</div>
</main>"""

DETAIL_BASELINE_1 = """<main>
<div class="project-header"><h1>Baseline Project One</h1></div>
<div class="project-body-description"><p>Description for baseline 1 containing full details and prose.</p></div>
</main>"""

DETAIL_BASELINE_2 = """<main>
<div class="project-header"><h1>Baseline Project Two</h1></div>
<div class="project-body-description"><p>Description for baseline 2 containing full details and prose.</p></div>
</main>"""

DETAIL_NEW_PROJECT = """<main>
<div class="project-header">
    <h1>New Hero Project</h1>
    <div class="project-header-info-list"><span class="badge">Rozenburg, Netherlands</span></div>
</div>
<div class="project-body-description">
    <p>This is a complete and detailed project description body for the new project containing more than fifty characters to pass validation easily.</p>
</div>
</main>"""


class SmokeTestBrowser:
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


def run_smoke_test():
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        db_path = tmp_path / "test_smoke.db"
        lock_path = tmp_path / "test_smoke.lock"
        
        # Patch Config and database paths to isolated temporary location
        with patch.object(Config, "DATABASE_PATH", db_path), \
             patch.object(database, "DATABASE_PATH", db_path), \
             patch.object(Config, "LOCK_PATH", lock_path), \
             patch.object(Config, "PRIMARY_SEARCH_URL", "https://www.freelancermap.com/projects?sort=1"), \
             patch.object(Config, "SMTP_FROM_EMAIL", "smoke-test@example.com"), \
             patch.object(Config, "AUTO_BASELINE_ON_FIRST_RUN", True):
            
            # Step 1: Initialize fresh database
            database.initialize_database()
            print("Step 1: Database initialized successfully at", db_path)

            # Step 2 & 3: Run Cycle 1 (Baseline)
            print("\nStep 2: Running Cycle 1 (Baseline scan)...")
            browser_1 = lambda headless=None: SmokeTestBrowser(
                HTML_CYCLE_1,
                {"baseline-1": DETAIL_BASELINE_1, "baseline-2": DETAIL_BASELINE_2}
            )

            with patch("monitor.BrowserSession", browser_1), \
                 patch("emailer._send") as mock_smtp_send, \
                 patch("emailer._validate_smtp"):
                res1 = run_cycle(force_baseline=True)
                print(f"Cycle 1 Result: baseline={res1.baseline}, discovered={res1.discovered}, new={res1.new}, emailed={res1.emailed}")
                assert res1.baseline is True
                assert res1.discovered == 2
                assert res1.new == 2
                assert res1.emailed == 0
                assert mock_smtp_send.call_count == 0
                print("Step 3: Confirmed 0 emails sent during baseline scan.")

            # Step 4-8: Run Cycle 2 (New Project Discovered & Emailed)
            print("\nStep 4: Running Cycle 2 (New project scan)...")
            browser_2 = lambda headless=None: SmokeTestBrowser(
                HTML_CYCLE_2,
                {"baseline-1": DETAIL_BASELINE_1, "baseline-2": DETAIL_BASELINE_2, "new-hero-project": DETAIL_NEW_PROJECT}
            )

            send_calls = []

            def mock_send(message, recipients=None):
                send_calls.append(message)
                return None

            with patch("monitor.BrowserSession", browser_2), \
                 patch("emailer._send", side_effect=mock_send), \
                 patch("emailer._validate_smtp"):
                res2 = run_cycle()
                print(f"Cycle 2 Result: baseline={res2.baseline}, discovered={res2.discovered}, new={res2.new}, emailed={res2.emailed}")
                assert res2.baseline is False
                assert res2.discovered == 3
                assert res2.new == 1
                assert res2.emailed == 1
                assert len(send_calls) == 1
                print("Step 5-8: Confirmed new project stored, digest built, sent to mocked SMTP, and marked sent.")

            # Step 9-10: Run Cycle 3 (Unchanged scan)
            print("\nStep 9: Running Cycle 3 (Unchanged scan)...")
            with patch("monitor.BrowserSession", browser_2), \
                 patch("emailer._send") as mock_smtp_send, \
                 patch("emailer._validate_smtp"):
                res3 = run_cycle()
                print(f"Cycle 3 Result: baseline={res3.baseline}, discovered={res3.discovered}, new={res3.new}, emailed={res3.emailed}")
                assert res3.baseline is False
                assert res3.discovered == 3
                assert res3.new == 0
                assert res3.emailed == 0
                assert mock_smtp_send.call_count == 0
                print("Step 10: Confirmed 0 duplicate rows inserted and 0 duplicate emails sent.")

            # Print final database row summary with secrets redacted
            print("\n--- Final Database Row Summary (Secrets Redacted) ---")
            with database.connection() as conn:
                projects = conn.execute("SELECT id, title, location, duration, workload, rate, email_status, emailed_at FROM projects ORDER BY id").fetchall()
                print(f"Total projects stored: {len(projects)}")
                for p in projects:
                    print(f"  [ID {p['id']}] Title: '{p['title']}' | Location: '{p['location']}' | Email Status: '{p['email_status']}' | Emailed At: '{p['emailed_at']}'")
                
                scans = conn.execute("SELECT id, status, discovered_count, new_count, emailed_count FROM scans ORDER BY id").fetchall()
                print(f"\nTotal scan cycles logged: {len(scans)}")
                for s in scans:
                    print(f"  [Scan {s['id']}] Status: '{s['status']}' | Discovered: {s['discovered_count']} | New: {s['new_count']} | Emailed: {s['emailed_count']}")

    print("\nISOLATED END-TO-END SMOKE TEST PASSED CLEANLY!")


if __name__ == "__main__":
    run_smoke_test()
