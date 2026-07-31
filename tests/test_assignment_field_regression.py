from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import database
from parser import (
    parse_listing_cards,
    parse_project_detail,
    merge_card_and_detail,
)

BASE = "https://www.freelancermap.com"
SCAN = "2026-07-31T02:00:00+00:00"


class AssignmentFieldRegressionTests(unittest.TestCase):
    def test_split_city_and_country_are_joined(self):
        html = """
        <div class="project-card">
          <div>Stealth IT Consulting</div>
          <a data-testid="title" href="/project/x">Automation Tester</a>
          <div data-testid="city">
            <a>Not Specified,</a><a>United Kingdom</a>
          </div>
          <div data-testid="remoteInPercent">On-site</div>
          <div data-testid="type">Agency contract (e.g. ANÜ)</div>
          <div data-testid="duration">6 months+</div>
          <div data-testid="startDate">ASAP</div>
          <div data-testid="workload">Full-time</div>
        </div>
        """
        card = parse_listing_cards(html, BASE, SCAN)[0]
        self.assertEqual("Not Specified, United Kingdom", card.card_location)
        self.assertEqual("6 months+", card.card_duration)
        self.assertEqual("Full-time", card.card_workload)
        self.assertEqual("Full-time", merge_card_and_detail(card, None).engagement_type)

    def test_description_location_does_not_swallow_following_fields(self):
        html = """
        <main>
          <h1>SAP EWM Consultant</h1>
          <a href="/project-provider/dsr">DSR Global</a>
          <div>Contact person: Ned Hayes</div>
          <div>Prague, <span>Czech Republic</span></div>
          <div>100% remote Freelance ASAP Duration 12 months 100% workload</div>
          <h2>Report project</h2>
          <h2>Description</h2>
          <p>Location: Remote</p>
          <p>Languages: English + Czech speaking</p>
          <p>Type: Contract/Freelance</p>
        </main>
        """
        detail = parse_project_detail(html, BASE + "/project/sap-ewm", BASE, SCAN)
        self.assertEqual("Prague, Czech Republic", detail.location)
        self.assertNotIn("Languages:", detail.location)
        self.assertEqual("100% workload", detail.engagement_type)

    def test_remote_percentage_is_not_workload(self):
        html = """
        <main><h1>Product Owner</h1>
          <a href="/provider">Provider</a>
          <div>Contact person: Person</div><div>Hannover, Germany</div>
          <div>80% remote Agency contract (e.g. ANÜ) ASAP Duration 14 months 100% workload</div>
          <h2>Description</h2><p>Role body.</p>
        </main>
        """
        detail = parse_project_detail(html, BASE + "/project/po", BASE, SCAN)
        self.assertEqual("80% remote", detail.workplace)
        self.assertEqual("100% workload", detail.workload)
        self.assertEqual("100% workload", detail.engagement_type)

    def test_absolute_date_and_clock_are_not_reduced_to_scan_day(self):
        html = """
        <head><script type="application/ld+json">
        {"@type":"JobPosting","url":"/project/d","title":"D",
         "datePosted":"30.07.2026 21:03"}
        </script></head>
        <main><h1>D</h1><h2>Description</h2><p>Body</p></main>
        """
        detail = parse_project_detail(html, BASE + "/project/d", BASE, "2026-07-31T02:00:00+00:00")
        self.assertEqual("2026-07-30T21:03:00+00:00", detail.posted_at)

    def test_database_stores_workload_as_engagement_and_contract_separately(self):
        original = database.DATABASE_PATH
        with tempfile.TemporaryDirectory() as folder:
            database.DATABASE_PATH = Path(folder) / "test.db"
            try:
                database.initialize_database()
                html = """
                <div class="project-card"><div>Provider</div>
                  <a data-testid="title" href="/project/db-x">Role</a>
                  <div data-testid="city">Berlin, Germany</div>
                  <div data-testid="type">Freelance</div>
                  <div data-testid="workload">Full-time</div>
                </div>
                """
                card = parse_listing_cards(html, BASE, SCAN)[0]
                project_id, _ = database.upsert_discovery(card)
                with database.connection() as conn:
                    row = conn.execute(
                        "SELECT engagement_type, contract_type, workload, location "
                        "FROM projects WHERE id = ?", (project_id,)
                    ).fetchone()
                self.assertEqual("Full-time", row["engagement_type"])
                self.assertEqual("Freelance", row["contract_type"])
                self.assertEqual("Full-time", row["workload"])
                self.assertEqual("Berlin, Germany", row["location"])
            finally:
                database.DATABASE_PATH = original

    def test_database_initialization_repairs_old_misparsed_fields(self):
        original = database.DATABASE_PATH
        with tempfile.TemporaryDirectory() as folder:
            database.DATABASE_PATH = Path(folder) / "test.db"
            try:
                database.initialize_database()
                html = """
                <div class="project-card"><div>Provider</div>
                  <a data-testid="title" href="/project/repair-x">Role</a>
                  <div data-testid="city">Prague, Czech Republic</div>
                  <div data-testid="type">Freelance</div>
                  <div data-testid="workload">Full-time</div>
                </div>
                """
                card = parse_listing_cards(html, BASE, SCAN)[0]
                project_id, _ = database.upsert_discovery(card)
                with database.connection(write=True) as conn:
                    conn.execute(
                        """
                        UPDATE projects SET
                            engagement_type = 'Freelance',
                            location = 'Remote Languages: English Type: Contract/Freelance',
                            detail_fetch_status = 'success'
                        WHERE id = ?
                        """,
                        (project_id,),
                    )
                    conn.execute("PRAGMA user_version = 6")

                database.initialize_database()

                with database.connection() as conn:
                    row = conn.execute(
                        "SELECT engagement_type, location, detail_fetch_status "
                        "FROM projects WHERE id = ?",
                        (project_id,),
                    ).fetchone()
                self.assertEqual("Full-time", row["engagement_type"])
                self.assertEqual("Remote", row["location"])
                self.assertEqual("pending", row["detail_fetch_status"])
            finally:
                database.DATABASE_PATH = original



if __name__ == "__main__":
    unittest.main()