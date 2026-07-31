from __future__ import annotations

import json
import unittest

from parser import parse_listing_cards, parse_project_detail

BASE = "https://www.freelancermap.com"
SCAN = "2026-07-31T04:00:00+00:00"


class ProductionHardeningTests(unittest.TestCase):
    def detail(self, body: str, slug: str = "production-test"):
        return parse_project_detail(body, f"{BASE}/project/{slug}", BASE, SCAN)

    def test_active_detail_modal_isolated_from_search_and_similar_cards(self):
        html = """
        <html><body>
          <main><h1>Find the perfect project</h1>
            <div class="project-card"><a href="/project/selected">Selected</a></div>
          </main>
          <div class="modal search-result-modal show" aria-hidden="false">
            <div class="project-show-date-created-design-b">#77 Published on 07/30/2026, 11:38 PM</div>
            <div class="grid-project-show"><div class="content">
              <div class="project-header"><h1>Selected</h1>
                <a data-id="project-show-company-link" href="/company/acme">Acme</a>
                <div class="project-info-name">Jane Doe</div>
                <div class="project-header-info-list">
                  <span class="badge"><i class="fa-location-pin"></i>Paris, France</span>
                  <span class="badge"><i class="fa-car-side"></i>Hybrid</span>
                  <span class="badge"><i class="fa-file-contract"></i>Freelance</span>
                </div>
              </div>
              <div class="project-body"><h2>Description</h2>
                <div class="project-body-description">Main project body.</div>
              </div>
              <div data-testid="similar-projects"><div class="project-card">
                <a href="/project/unrelated">Unrelated</a><div>Duration 99 months</div>
              </div></div>
            </div></div>
          </div>
        </body></html>
        """
        detail = self.detail(html, "selected")
        self.assertEqual("Selected", detail.title)
        self.assertEqual("Acme", detail.company)
        self.assertEqual("Jane Doe", detail.contact_person)
        self.assertEqual("Paris, France", detail.location)
        self.assertEqual("Hybrid", detail.workplace)
        self.assertEqual("Freelance", detail.contract_type)
        self.assertEqual("", detail.duration)
        self.assertNotIn("Unrelated", detail.description)
        self.assertEqual("2026-07-30T23:38:00+00:00", detail.posted_at)

    def test_hidden_stale_modal_does_not_beat_visible_direct_detail(self):
        html = """
        <main><div class="grid-project-show"><div class="content">
          <div class="project-header"><h1>Visible Project</h1></div>
          <div class="project-body"><h2>Description</h2>
            <div class="project-body-description">Visible body.</div></div>
        </div></div></main>
        <div class="modal search-result-modal hidden" aria-hidden="true">
          <div class="grid-project-show"><div class="project-header"><h1>Stale Project</h1></div>
          <div class="project-body-description">Stale body.</div></div>
        </div>
        """
        detail = self.detail(html, "visible-project")
        self.assertEqual("Visible Project", detail.title)
        self.assertIn("Visible body", detail.description)
        self.assertNotIn("Stale", detail.description)

    def test_card_created_date_is_not_misclassified_as_start_date(self):
        html = """
        <div class="project-card"><div class="project-info">
          <div>Provider</div><a data-testid="title" href="/project/no-start">No Start</a>
          <div data-testid="city"><a>London,</a><a>United Kingdom</a></div>
          <div data-testid="type">Freelance</div>
        </div><div class="project-created"><span data-testid="created">30.07.2026</span></div></div>
        """
        project = parse_listing_cards(html, BASE, SCAN)[0]
        self.assertEqual("30.07.2026", project.posted_text)
        self.assertEqual("", project.card_start_date)

    def test_card_beginning_text_is_used_as_start_date(self):
        html = """
        <div class="project-card"><div class="project-info">
          <div>Provider</div><a data-testid="title" href="/project/with-start">With Start</a>
          <div data-testid="beginningText">ASAP</div>
        </div><span data-testid="created">30.07.2026</span></div>
        """
        project = parse_listing_cards(html, BASE, SCAN)[0]
        self.assertEqual("ASAP", project.card_start_date)
        self.assertEqual("30.07.2026", project.posted_text)

    def test_card_beginning_month_and_month_window_are_preserved(self):
        numeric = """
        <div class="project-card"><a data-testid="title" href="/project/numeric-start">Numeric</a>
          <div data-testid="beginningMonth">7/2026</div><span data-testid="created">30.07.2026</span></div>
        """
        window = """
        <div class="project-card"><a data-testid="title" href="/project/window-start">Window</a>
          <div data-testid="beginningText">August/September</div><span data-testid="created">30.07.2026</span></div>
        """
        self.assertEqual("7/2026", parse_listing_cards(numeric, BASE, SCAN)[0].card_start_date)
        self.assertEqual("August/September", parse_listing_cards(window, BASE, SCAN)[0].card_start_date)

    def test_stale_state_only_project_is_not_discovered_when_dom_cards_exist(self):
        state = {"initialState": {"result": {"projects": [
            {"slug": "visible", "title": "Visible", "links": {"project": "/project/visible"}},
            {"slug": "stale", "title": "Stale", "links": {"project": "/project/stale"}},
        ]}}}
        html = f"""
        <div class="project-card"><a data-testid="title" href="/project/visible">Visible</a></div>
        <script type="application/json" data-component-name="ProjectSearch">{json.dumps(state)}</script>
        """
        projects = parse_listing_cards(html, BASE, SCAN)
        self.assertEqual(["visible"], [project.slug for project in projects])

    def test_dom_card_and_projectsearch_state_are_merged_by_source_priority(self):
        state = {
            "initialState": {"result": {"projects": [{
                "slug": "merged-project",
                "title": "Merged Project",
                "company": "State Provider",
                "description": "<p>Full embedded description.</p>",
                "city": "London",
                "country": {"name": "United Kingdom"},
                "created": "2026-07-30T20:01:30+02:00",
                "durationText": "6 months",
                "beginningText": "ASAP",
                "projectContractType": {"type": "contracting", "remoteInPercent": 0},
                "links": {"project": "/project/merged-project"},
            }]}}
        }
        html = f"""
        <div class="project-card"><div class="project-info">
          <div>Visible Provider</div>
          <a data-testid="title" href="/project/merged-project">Merged Project</a>
          <div data-testid="city"><a>London,</a><a>United Kingdom</a></div>
          <div data-testid="remoteInPercent">Hybrid</div>
          <div data-testid="type">Freelance</div>
          <div data-testid="duration">6 months+</div>
          <div data-testid="beginningText">ASAP</div>
        </div><span data-testid="created">30.07.2026</span></div>
        <script type="application/json" data-component-name="ProjectSearch">{json.dumps(state)}</script>
        """
        projects = parse_listing_cards(html, BASE, SCAN)
        self.assertEqual(1, len(projects))
        project = projects[0]
        self.assertEqual("Visible Provider", project.company_hint)
        self.assertEqual("Hybrid", project.card_workplace)
        self.assertEqual("6 months+", project.card_duration)
        self.assertEqual("Full embedded description.", project.card_description)
        self.assertEqual("2026-07-30T20:01:30+02:00", project.posted_text)
        self.assertEqual("2026-07-30T18:01:30+00:00", project.posted_at)
        self.assertTrue(project.card_html)

    def test_actual_projectsearch_json_shape_is_supported_without_dom_cards(self):
        state = {
            "initialState": {"result": {"projects": [{
                "id": 123,
                "slug": "state-project",
                "title": "State Project",
                "company": "State Provider",
                "description": "<p>State description</p>",
                "city": "Berlin",
                "country": {"name": "Germany"},
                "created": "2026-07-30T20:01:30+02:00",
                "beginningText": "ASAP",
                "durationText": "6 months",
                "projectContractType": {"type": "contracting", "remoteInPercent": 100},
                "links": {"project": "/project/state-project"},
                "embedding": [0.1] * 1000,
                "matching": {"large": [1] * 1000},
            }]}}
        }
        html = (
            '<script type="application/json" data-component-name="ProjectSearch">'
            + json.dumps(state)
            + "</script>"
        )
        projects = parse_listing_cards(html, BASE, SCAN)
        self.assertEqual(1, len(projects))
        project = projects[0]
        self.assertEqual("State Project", project.title_hint)
        self.assertEqual("State Provider", project.company_hint)
        self.assertEqual("Berlin, Germany", project.card_location)
        self.assertEqual("100% remote", project.card_workplace)
        self.assertEqual("Freelance", project.card_contract_type)
        self.assertEqual("6 months", project.card_duration)
        self.assertEqual("ASAP", project.card_start_date)
        self.assertEqual("2026-07-30T18:01:30+00:00", project.posted_at)
        self.assertLess(len(project.card_text), 1000)

    def test_json_ld_employment_list_remote_and_multiple_locations(self):
        payload = {
            "@context": "https://schema.org",
            "@type": "JobPosting",
            "url": f"{BASE}/project/jsonld-project",
            "title": "JSON-LD Project",
            "description": "<p>Structured body</p>",
            "employmentType": ["FULL_TIME", "CONTRACTOR"],
            "jobLocationType": "TELECOMMUTE",
            "jobLocation": [
                {"address": {"addressLocality": "Madrid", "addressCountry": "Spain"}},
                {"address": {"addressLocality": "Lisbon", "addressCountry": "Portugal"}},
            ],
            "baseSalary": {"currency": "EUR", "value": {"minValue": 500, "maxValue": 600, "unitText": "DAY"}},
        }
        html = f'''<script type="application/ld+json">{json.dumps(payload)}</script>
        <main><h1>JSON-LD Project</h1><h2>Description</h2><p>Visible body</p></main>'''
        detail = self.detail(html, "jsonld-project")
        self.assertEqual("Full-time", detail.workload)
        self.assertEqual("Freelance", detail.contract_type)
        self.assertEqual("Remote", detail.workplace)
        self.assertEqual("Madrid, Spain", detail.location)
        self.assertEqual(["Lisbon, Portugal"], detail.raw_metadata.get("alternate_locations"))
        self.assertEqual("EUR 500-600 DAY", detail.rate)

    def test_header_location_wins_over_description_attendance_location(self):
        html = """
        <main><div class="grid-project-show"><div class="content">
          <div class="project-header"><h1>Location Priority</h1>
            <div class="project-header-info-list"><span class="badge">Riad, Saudi Arabia</span></div>
          </div>
          <div class="project-body"><h2>Description</h2><div class="project-body-description">
            Work location: Remote, on-site in Saudi Arabia every 2 weeks
          </div></div>
        </div></div></main>
        """
        detail = self.detail(html, "location-priority")
        self.assertEqual("Riad, Saudi Arabia", detail.location)
        self.assertEqual("Riad", detail.city)
        self.assertEqual("Saudi Arabia", detail.country)

    def test_specific_description_location_replaces_generic_header(self):
        html = """
        <main><div class="grid-project-show"><div class="content">
          <div class="project-header"><h1>Specific Location</h1>
            <div class="project-header-info-list"><span class="badge">Not Specified</span></div>
          </div>
          <div class="project-body"><h2>Description</h2><div class="project-body-description">
            Location: Helsinki, Finland
          </div></div>
        </div></div></main>
        """
        detail = self.detail(html, "specific-location")
        self.assertEqual("Helsinki, Finland", detail.location)

    def test_description_location_parenthetical_remote_is_preserved(self):
        html = """
        <main><h1>Product Designer</h1><h2>Description</h2>
          <div class="project-body-description">Location: Finland (remote)</div>
        </main>
        """
        detail = self.detail(html, "product-designer")
        self.assertEqual("Finland (remote)", detail.location)
        self.assertEqual("Remote", detail.workplace)

    def test_adjacent_prose_is_never_accepted_as_a_fact(self):
        html = """
        <main><h1>Boundary</h1><div class="project-header">
          <div>Workload</div><div>The successful candidate leads delivery.</div>
          <div>Contract type</div><div>The role supports a global programme.</div>
          <div>Duration</div><div>Responsibilities include planning.</div>
          <div>Rate</div><div>The successful applicant negotiates suppliers.</div>
        </div><h2>Description</h2><p>Body.</p></main>
        """
        detail = self.detail(html, "boundary")
        self.assertEqual("", detail.workload)
        self.assertEqual("", detail.contract_type)
        self.assertEqual("", detail.duration)
        self.assertEqual("", detail.rate)

    def test_rate_matrix_preserves_qualifiers_and_ignores_ir35_number(self):
        cases = {
            "Pay Rate: Competitive\nIR35 Status: Outside IR35": "Competitive",
            "Day Rate: Up to £350 a day (Inside IR35)": "Up to £350 a day (Inside IR35)",
            "Rate: Circa 150 Euro per hour": "Circa 150 Euro per hour",
            "Glasgow - £674 p/d": "£674 p/d",
            "Rate: £21.83 per hour + holidays PAYE": "£21.83 per hour + holidays PAYE",
            "Hourly rate: 60-70€": "60-70€",
            "Rate: Open, please share your expectations": "Open",
        }
        for index, (text, expected) in enumerate(cases.items()):
            with self.subTest(text=text):
                html = f"<main><h1>Rate {index}</h1><h2>Description</h2><div class='project-body-description'>{text}</div></main>"
                detail = self.detail(html, f"rate-{index}")
                self.assertEqual(expected, detail.rate)
                self.assertNotEqual("35", detail.rate)

    def test_duration_matrix_is_bounded(self):
        cases = {
            "Duration: 6 months initial contract Workload: Full-time": "6 months initial contract",
            "Contract Length: Initial 3 months": "Initial 3 months",
            "Duration: 12 months (extension possible for 1 year)": "12 months (extension possible for 1 year)",
            "Project Length: 6+ months": "6+ months",
            "Duration: Long-term contract": "Long-term contract",
        }
        for index, (text, expected) in enumerate(cases.items()):
            with self.subTest(text=text):
                html = f"<main><h1>Duration {index}</h1><h2>Description</h2><div class='project-body-description'>{text}</div></main>"
                detail = self.detail(html, f"duration-{index}")
                self.assertEqual(expected, detail.duration)
                self.assertNotIn("Workload", detail.duration)

    def test_attendance_days_are_not_workload_without_explicit_label(self):
        html = """
        <main><h1>Attendance</h1><h2>Description</h2><div class="project-body-description">
          London: Hybrid - 1-2 days per week on-site
        </div></main>
        """
        detail = self.detail(html, "attendance")
        self.assertEqual("Hybrid", detail.workplace)
        self.assertEqual("", detail.workload)

    def test_explicit_days_per_week_is_valid_workload(self):
        html = """
        <main><h1>Allocation</h1><h2>Description</h2><div class="project-body-description">
          Workload: 3 days per week
        </div></main>
        """
        detail = self.detail(html, "allocation")
        self.assertEqual("3 days per week", detail.workload)

    def test_description_renderer_keeps_headings_and_bullets_without_dangling_markers(self):
        html = """
        <main><h1>Formatting</h1><div class="project-body"><h2>Description</h2>
          <div class="project-body-description">Formatting<br><br>Responsibilities<br><ul>
            <li><br><br>First task</li><li>Second task</li></ul>Requirements<ul><li>One skill</li></ul>
          </div></div></main>
        """
        detail = self.detail(html, "formatting")
        self.assertTrue(detail.description.startswith("Responsibilities"))
        self.assertIn("- First task", detail.description)
        self.assertIn("- Second task", detail.description)
        self.assertNotIn("\n-\n", detail.description)

    def test_hidden_report_dialog_cannot_pollute_contract_or_rate(self):
        html = """
        <main><div class="grid-project-show"><div class="content">
          <div class="project-header"><h1>Clean</h1><span class="badge">Freelance</span>
            <div class="modal project-header-report-modal hidden" aria-hidden="true">
              Incorrect contract type The hourly rate is unrealistic
            </div>
          </div>
          <div class="project-body"><h2>Description</h2><div class="project-body-description">Clean body.</div></div>
        </div></div></main>
        """
        detail = self.detail(html, "clean")
        self.assertEqual("Freelance", detail.contract_type)
        self.assertEqual("", detail.rate)
        self.assertNotIn("unrealistic", detail.description.casefold())

    def test_hidden_ancestor_marks_nested_stale_detail_as_hidden(self):
        html = """
        <main><div class="grid-project-show"><div class="project-header"><h1>Live Detail</h1></div>
          <div class="project-body-description">Live description.</div></div></main>
        <div style="display:none"><div class="grid-project-show">
          <div class="project-header"><h1>Hidden Ancestor Detail</h1></div>
          <div class="project-body-description">Hidden description.</div>
        </div></div>
        """
        detail = self.detail(html, "live-detail")
        self.assertEqual("Live Detail", detail.title)
        self.assertNotIn("Hidden", detail.description)

    def test_generic_search_page_is_not_accepted_as_project_detail(self):
        html = """
        <html><head>
          <title>Freelance Jobs & IT Projects Worldwide | freelancermap</title>
          <meta name="description" content="Find freelance jobs and IT projects worldwide — contracts updated daily. Browse now.">
        </head><body><main><h1>Find the perfect project</h1></main></body></html>
        """
        detail = self.detail(html, "missing-project")
        self.assertEqual("", detail.title)
        self.assertEqual("", detail.description)
        self.assertIn("missing_title", detail.raw_metadata["parser"]["warnings"])

    def test_parser_records_compact_field_provenance(self):
        html = """
        <main><div class="grid-project-show"><div class="project-header">
          <h1>Provenance Project</h1><div class="project-header-info-list">
            <span class="badge">Berlin, Germany</span><span class="badge">Hybrid</span>
          </div></div><div class="project-body-description">Body.</div></div></main>
        """
        detail = self.detail(html, "provenance-project")
        sources = detail.raw_metadata["parser"]["field_sources"]
        self.assertEqual("visible_detail", sources["title"])
        self.assertEqual("visible_detail", sources["location"])
        self.assertEqual("visible_detail", sources["workplace"])
        self.assertNotIn("", sources)

    def test_parser_diagnostics_are_small_and_actionable(self):
        detail = self.detail("<main><h1>Diagnostic</h1><h2>Description</h2><p>Body.</p></main>", "diagnostic")
        parser_meta = detail.raw_metadata["parser"]
        self.assertIn("version", parser_meta)
        self.assertIn("detail_scope", parser_meta)
        self.assertIn("field_sources", parser_meta)
        self.assertEqual([], parser_meta["warnings"])
        self.assertLess(len(json.dumps(detail.raw_metadata)), 6000)


if __name__ == "__main__":
    unittest.main()