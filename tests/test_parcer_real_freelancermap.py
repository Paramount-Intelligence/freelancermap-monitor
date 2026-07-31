from __future__ import annotations

import unittest
from pathlib import Path

from parser import (
    parse_listing_cards,
    parse_project_detail,
    parse_relative_posted_time,
)

BASE = "https://www.freelancermap.com"
SCAN = "2026-07-31T04:00:00+00:00"
FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "freelancermap_site_engineer_real.html"
)


class CapturedFreelancermapDomTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = FIXTURE.read_text(encoding="utf-8")

    def test_listing_ignores_open_detail_modal_and_similar_projects(self):
        projects = parse_listing_cards(self.html, BASE, SCAN)
        by_slug = {project.slug: project for project in projects}

        self.assertEqual({"site-engineer-ii"}, set(by_slug))
        project = by_slug["site-engineer-ii"]
        self.assertEqual("Site Engineer II", project.title_hint)
        self.assertEqual("Darwin Recruitment", project.company_hint)
        self.assertEqual("Rozenburg, Netherlands", project.card_location)
        self.assertEqual("On-site", project.card_workplace)
        self.assertEqual("Freelance", project.card_contract_type)
        self.assertEqual("7/2026", project.card_start_date)
        self.assertEqual("5 months+", project.card_duration)
        self.assertEqual("24.04.2026", project.posted_text)
        self.assertEqual("2026-04-24T00:00:00+00:00", project.posted_at)

    def test_real_search_result_detail_modal_is_parsed_in_isolation(self):
        detail = parse_project_detail(
            self.html,
            BASE + "/project/site-engineer-ii",
            BASE,
            SCAN,
        )

        self.assertEqual("Site Engineer II", detail.title)
        self.assertEqual("Darwin Recruitment", detail.company)
        self.assertEqual("Alex Deery", detail.contact_person)
        self.assertEqual("Rozenburg, Netherlands", detail.location)
        self.assertEqual("Rozenburg", detail.city)
        self.assertEqual("Netherlands", detail.country)
        self.assertEqual("On-site", detail.workplace)
        self.assertEqual("Freelance", detail.contract_type)
        self.assertEqual("7/2026", detail.start_date)
        self.assertEqual("", detail.duration)
        self.assertEqual("", detail.workload)
        self.assertEqual("", detail.rate)
        self.assertEqual(
            "07/30/2026, 11:38 PM",
            detail.posted_text,
        )
        self.assertEqual(
            "2026-07-30T23:38:00+00:00",
            detail.posted_at,
        )

        self.assertIn("Responsibilities", detail.description)
        self.assertIn("Requirements", detail.description)
        self.assertNotIn("Similar projects", detail.description)
        self.assertNotIn("Incorrect contract type", detail.description)
        self.assertNotIn("The hourly rate is unrealistic", detail.description)

        # These values exist only in the modal's Similar projects section.
        # They must not leak into the selected project.
        self.assertNotEqual("12 months+", detail.duration)
        self.assertNotEqual("asap", detail.start_date.casefold())

    def test_missing_adjacent_values_do_not_consume_prose(self):
        html = """
        <main>
          <h1>Boundary Test</h1>
          <div class="project-header">
            <div>Workload</div>
            <div>The successful candidate will lead delivery.</div>
            <div>Contract type</div>
            <div>The role supports a global programme.</div>
          </div>
          <div class="project-body">
            <h2>Description</h2>
            <div class="project-body-description">
              The successful candidate will lead delivery.
            </div>
          </div>
        </main>
        """
        detail = parse_project_detail(
            html,
            BASE + "/project/boundary-test",
            BASE,
            SCAN,
        )
        self.assertEqual("", detail.workload)
        self.assertEqual("", detail.contract_type)
        self.assertNotEqual("The", detail.workload)
        self.assertNotEqual("The", detail.contract_type)

    def test_realistic_description_facts_are_bounded_and_cleaned(self):
        html = """
        <main>
          <div class="grid-project-show"><div class="content">
            <div class="project-header">
              <h1>ETCS Consultant</h1>
              <a data-id="project-show-company-link"
                 href="/project-provider/example">Provider</a>
              <div class="project-header-info-list">
                <span class="badge">Riad, Saudi Arabia</span>
                <span class="badge">40% remote</span>
                <span class="badge">Freelance</span>
              </div>
            </div>
            <div class="project-body">
              <h2>Description</h2>
              <div class="project-body-description">
                <p>Contract type: Freelance</p>
                <p>Start: Immediately</p>
                <p>Duration: 12 months (extension possible for 1 year)</p>
                <p>Work location: Remote, on-site in Saudi Arabia every 2 weeks</p>
                <p>Workload: Full-time</p>
                <p>Hourly rate: 60-70€</p>
              </div>
            </div>
          </div></div>
        </main>
        """
        detail = parse_project_detail(
            html,
            BASE + "/project/etcs-consultant",
            BASE,
            SCAN,
        )
        self.assertEqual("Riad, Saudi Arabia", detail.location)
        self.assertEqual("40% remote", detail.workplace)
        self.assertEqual("Freelance", detail.contract_type)
        self.assertEqual("Immediately", detail.start_date)
        self.assertEqual(
            "12 months (extension possible for 1 year)",
            detail.duration,
        )
        self.assertEqual("Full-time", detail.workload)
        self.assertEqual("60-70€", detail.rate)


    def test_rate_values_keep_units_and_ignore_ir35_digits(self):
        cases = (
            ("Pay Rate: Competitive\nIR35 Status: Outside IR35", "Competitive"),
            ("Day Rate: Up to £350 a day (Inside IR35)", "Up to £350 a day (Inside IR35)"),
            ("Rate: Circa 150 Euro per hour", "Circa 150 Euro per hour"),
            ("Glasgow - £674 p/d", "£674 p/d"),
            ("Rate: £21.83 per hour + holidays PAYE", "£21.83 per hour + holidays PAYE"),
        )
        for index, (description, expected) in enumerate(cases):
            with self.subTest(description=description):
                html = f"""
                <main>
                  <div class="grid-project-show"><div class="content">
                    <div class="project-header"><h1>Rate Test {index}</h1></div>
                    <div class="project-body">
                      <h2>Description</h2>
                      <div class="project-body-description">
                        {description.replace(chr(10), '<br>')}
                      </div>
                    </div>
                  </div></div>
                </main>
                """
                detail = parse_project_detail(
                    html,
                    BASE + f"/project/rate-test-{index}",
                    BASE,
                    SCAN,
                )
                self.assertEqual(expected, detail.rate)
                self.assertNotEqual("35", detail.rate)

    def test_duration_stops_at_the_next_field_and_keeps_qualifiers(self):
        html = """
        <main>
          <div class="grid-project-show"><div class="content">
            <div class="project-header"><h1>Duration Boundaries</h1></div>
            <div class="project-body">
              <h2>Description</h2>
              <div class="project-body-description">
                Duration: 6 months initial contract
                Workload: Full-time
                Rate: €500-€550 per day
              </div>
            </div>
          </div></div>
        </main>
        """
        detail = parse_project_detail(
            html,
            BASE + "/project/duration-boundaries",
            BASE,
            SCAN,
        )
        self.assertEqual("6 months initial contract", detail.duration)
        self.assertEqual("Full-time", detail.workload)
        self.assertEqual("€500-€550 per day", detail.rate)
        self.assertNotIn("Workload", detail.duration)
        self.assertNotIn("Rate", detail.duration)

    def test_date_only_posting_uses_scan_timestamp(self):
        self.assertEqual(
            "2026-07-30T00:00:00+00:00",
            parse_relative_posted_time("30-Jul-2026", SCAN),
        )


if __name__ == "__main__":
    unittest.main()