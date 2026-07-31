from datetime import datetime, timezone
import unittest

from parser import (
    merge_card_and_detail,
    parse_listing_cards,
    parse_project_detail,
    parse_relative_posted_time,
    ProjectDetail,
    _extract_location_from_lines,
    _is_metadata_line,
    _parse_absolute_datetime,
    _split_location,
)

BASE = "https://www.freelancermap.com"
SCAN = "2026-07-31T00:00:00+00:00"


class EnhancedParserTests(unittest.TestCase):
    def test_exact_authenticated_data_testids_from_user_dom(self):
        html = '''
        <div id="project-search-result">
          <div class="project-list project-list-grid">
            <div class="project-card">
              <div class="project-info">
                <div>Stealth IT Consulting</div>
                <div>
                  <a data-testid="title" data-id="project-card-title"
                     href="/project/automation-tester-active-sc-400-inside?track=1">
                     Automation Tester - Active SC - £400 Inside
                  </a>
                </div>
                <div class="project-info-list">
                  <div data-testid="city">Not Specified, United Kingdom</div>
                  <div data-testid="remoteInPercent"><span>On-site</span></div>
                  <div data-testid="type"><span>Agency contract (e.g. ANÜ)</span></div>
                  <div data-testid="duration"><span>6 months</span></div>
                  <div data-testid="startDate"><span>ASAP</span></div>
                  <div data-testid="workload"><span>100% workload</span></div>
                </div>
              </div>
              <div data-testid="description"><p>Build and maintain reusable automation frameworks.</p></div>
              <div>3 hours ago · 1,234 views</div>
            </div>
          </div>
        </div>'''
        projects = parse_listing_cards(html, BASE, SCAN)
        self.assertEqual(1, len(projects))
        item = projects[0]
        self.assertEqual("Automation Tester - Active SC - £400 Inside", item.title_hint)
        self.assertEqual("Stealth IT Consulting", item.company_hint)
        self.assertEqual("Not Specified, United Kingdom", item.card_location)
        self.assertEqual("On-site", item.card_workplace)
        self.assertEqual("Agency contract (e.g. ANÜ)", item.card_contract_type)
        self.assertEqual("6 months", item.card_duration)
        self.assertEqual("ASAP", item.card_start_date)
        self.assertEqual("100% workload", item.card_workload)
        self.assertEqual(1234, item.view_count)
        self.assertEqual("2026-07-30T21:00:00+00:00", item.posted_at)
        self.assertNotIn("?", item.url)

    def test_react_json_state_is_a_conservative_fallback(self):
        html = '''<div id="hit-list"><script type="application/json"
          data-component-name="projectSearch">{
          "projects": [{
            "projectUrl": "/project/state-only-project",
            "projectTitle": "State Only Project",
            "providerName": "Provider",
            "description": "A complete project summary from embedded state.",
            "city": "Berlin, Germany",
            "remoteInPercent": "80% remote",
            "contractType": "Freelance",
            "duration": "12 months",
            "workload": "Full-time",
            "postedAt": "2 days ago",
            "viewCount": 88
          }]}</script></div>'''
        projects = parse_listing_cards(html, BASE, SCAN)
        self.assertEqual(1, len(projects))
        item = projects[0]
        self.assertEqual("State Only Project", item.title_hint)
        self.assertEqual("Provider", item.company_hint)
        self.assertEqual("Berlin, Germany", item.card_location)
        self.assertEqual("80% remote", item.card_workplace)
        self.assertEqual(88, item.view_count)

    def test_route_only_state_object_is_ignored(self):
        html = '''<script type="application/json" data-component-name="projectSearch">
        {"navigation":{"url":"/project/not-a-card"}}</script>'''
        self.assertEqual([], parse_listing_cards(html, BASE, SCAN))

    def test_public_compact_header_fields(self):
        html = '''<html><head><link rel="canonical" href="/project/product-owner"></head>
        <body><main><h1>Product Owner</h1><a href="/project-provider/robert-half">Robert Half</a>
        <div>Contact person: Fabienne Vergin</div><div>hannover, Germany</div>
        <div>80% remote Agency contract (e.g. ANÜ) asap Duration 14 months 100% workload</div>
        <button>Apply now</button><h2>Report project</h2><p>After submitting, you will receive a confirmation email.</p>
        <h2>Description</h2><p>Main role description.</p><h3>Tasks</h3><ul><li>Own the roadmap.</li></ul>
        </main></body></html>'''
        detail = parse_project_detail(html, BASE + "/project/product-owner", BASE, SCAN)
        self.assertEqual("hannover, Germany", detail.location)
        self.assertEqual("80% remote", detail.workplace)
        self.assertEqual("Agency contract (e.g. ANÜ)", detail.contract_type)
        self.assertEqual("ASAP", detail.start_date.upper())
        self.assertEqual("14 months", detail.duration)
        self.assertEqual("100% workload", detail.workload)
        self.assertEqual("Fabienne Vergin", detail.contact_person)
        self.assertIn("Main role description", detail.description)
        self.assertIn("Own the roadmap", detail.description)
        self.assertNotIn("After submitting", detail.description)

    def test_labeled_description_preserves_qualifiers(self):
        html = '''<main><h1>Program Manager</h1><div>Provider</div><h2>Description</h2>
        <p>Contract Details</p><ul>
        <li>Start Date: July 21, 2026 (including one week of training)</li>
        <li>Duration: Until the end of October 2026, with possible extension</li>
        <li>Workload: Full-time preferred</li>
        <li>Location: APAC region (travel expected as needed)</li>
        <li>Contract Type: Freelance</li>
        <li>Daily/Hourly Rate: TBD</li></ul></main>'''
        detail = parse_project_detail(html, BASE + "/project/program-manager", BASE, SCAN)
        self.assertEqual("July 21, 2026", detail.start_date)
        self.assertTrue(detail.duration.lower().startswith("until the end of october"))
        self.assertEqual("Full-time preferred", detail.workload)
        self.assertEqual("APAC region (travel expected as needed)", detail.location)
        self.assertEqual("Freelance", detail.contract_type)
        self.assertEqual("TBD", detail.rate)

    def test_best_matching_json_ld_object_is_selected(self):
        html = '''<head>
        <script type="application/ld+json">{"@graph":[
          {"@type":"JobPosting","url":"/project/wrong","title":"Wrong"},
          {"@type":"JobPosting","url":"/project/right","title":"Right","description":"<p>Right body</p>","datePosted":"2026-07-30"}
        ]}</script></head><body><main><h1>Right</h1><h2>Description</h2><p>Right body</p></main></body>'''
        detail = parse_project_detail(html, BASE + "/project/right", BASE, SCAN)
        self.assertEqual(BASE + "/project/right", detail.url)
        self.assertEqual("Right", detail.title)
        self.assertEqual("2026-07-30", detail.published_at)

    def test_relative_time_variants_and_future_fallback(self):
        self.assertEqual(
            "2026-07-30T23:00:00+00:00",
            parse_relative_posted_time("an hour ago", SCAN),
        )
        self.assertEqual(
            "2026-07-31T00:00:00+00:00",
            parse_relative_posted_time("less than a minute ago", SCAN),
        )
        self.assertEqual(
            "2026-07-31T00:00:00+00:00",
            parse_relative_posted_time("01-Aug-2026", SCAN),
        )
        self.assertEqual(
            "2026-07-30T00:00:00+00:00",
            parse_relative_posted_time("30-Jul-2026", SCAN),
        )

    def test_description_merge_adds_only_unique_card_lines(self):
        card_html = '''<div class="project-card"><a href="/project/x">X</a>
        <div data-testid="description"><p>Shared summary.</p><p>Card-only requirement.</p></div></div>'''
        card = parse_listing_cards(card_html, BASE, SCAN)[0]
        detail_html = '''<main><h1>X</h1><h2>Description</h2>
        <p>Shared summary.</p><p>Full detail text.</p></main>'''
        detail = parse_project_detail(detail_html, BASE + "/project/x", BASE, SCAN)
        merged = merge_card_and_detail(card, detail, SCAN)
        self.assertEqual(1, merged.description.count("Shared summary."))
        self.assertIn("Card-only requirement.", merged.description)
        self.assertIn("Full detail text.", merged.description)

    def test_public_listing_clock_time_is_parsed_as_posted_at(self):
        html = '''<div class="project-card">
        <div class="project-info"><div>Provider GmbH</div>
        <a data-testid="title" data-id="project-card-title" href="/project/today-project">Today Project</a>
        <div data-testid="city">Berlin, Germany</div>
        <div data-testid="remoteInPercent">100% remote</div>
        <div data-testid="type">Freelance</div>
        <div data-testid="duration">3 months+</div>
        <div data-testid="startDate">asap</div></div>
        <div data-testid="description">Useful project description for today.</div>
        <div>21:03</div></div>'''
        project = parse_listing_cards(html, BASE, "2026-07-31T22:00:00+00:00")[0]
        self.assertEqual("21:03", project.posted_text)
        self.assertEqual("2026-07-31T21:03:00+00:00", project.posted_at)

    def test_public_listing_clock_time_handles_midnight_rollover(self):
        self.assertEqual(
            "2026-07-30T23:55:00+00:00",
            parse_relative_posted_time("23:55", "2026-07-31T00:10:00+00:00"),
        )

    def test_public_listing_date_uses_last_card_date_not_start_date(self):
        html = '''<div class="project-card">
        <div>Provider GmbH</div>
        <a data-testid="title" href="/project/older-project">Older Project</a>
        <div data-testid="startDate">01.09.2026</div>
        <div data-testid="description">A sufficiently detailed project summary.</div>
        <div>29.07.2026</div></div>'''
        project = parse_listing_cards(html, BASE, SCAN)[0]
        self.assertEqual("29.07.2026", project.posted_text)
        self.assertEqual("2026-07-29T00:00:00+00:00", project.posted_at)

    def test_published_on_line_is_metadata_not_location(self):
        self.assertTrue(_is_metadata_line("Published on 07/31/2026, 08:34 PM"))
        self.assertTrue(_is_metadata_line("Published 07/31/2026"))
        self.assertEqual(
            "Berlin, Germany",
            _extract_location_from_lines(
                ["Published on 07/31/2026, 08:34 PM", "Berlin, Germany"]
            ),
        )

    def test_us_date_only_formats_parse(self):
        scan = datetime(2026, 7, 31, 21, 0, tzinfo=timezone.utc)
        self.assertEqual(
            datetime(2026, 7, 31, 0, 0, tzinfo=timezone.utc),
            _parse_absolute_datetime("07/31/2026", scan),
        )
        self.assertEqual(
            datetime(2026, 12, 31, 0, 0, tzinfo=timezone.utc),
            _parse_absolute_datetime("12/31/2026", scan),
        )
        self.assertEqual(
            datetime(2026, 11, 12, 0, 0, tzinfo=timezone.utc),
            _parse_absolute_datetime("11/12/2026", scan),
        )

    def test_eu_dates_still_parse_day_first(self):
        scan = datetime(2026, 7, 31, 21, 0, tzinfo=timezone.utc)
        self.assertEqual(
            datetime(2026, 7, 31, 0, 0, tzinfo=timezone.utc),
            _parse_absolute_datetime("31.07.2026", scan),
        )
        self.assertEqual(
            datetime(2026, 5, 13, 0, 0, tzinfo=timezone.utc),
            _parse_absolute_datetime("13/05/2026", scan),
        )

    def test_split_location_does_not_use_remote_as_city(self):
        detail = ProjectDetail(source_key="k", slug="k", url="u")
        detail.location = "Remote, United Kingdom"
        _split_location(detail)
        self.assertEqual("", detail.city)
        self.assertEqual("United Kingdom", detail.country)

    def test_json_ld_list_description_is_joined(self):
        html = '''
        <html><head>
        <script type="application/ld+json">{
          "@context": "https://schema.org",
          "@type": "JobPosting",
          "title": "Platform Engineer",
          "datePosted": "2026-07-31",
          "url": "/project/platform-engineer-1",
          "description": ["First paragraph.", "Second paragraph."],
          "hiringOrganization": {"@type": "Organization", "name": "Acme GmbH"}
        }</script>
        </head><body><main><h1>Platform Engineer</h1>
        <p>First paragraph.</p><p>Second paragraph.</p></main></body></html>
        '''
        detail = parse_project_detail(html, "https://www.freelancermap.com/project/platform-engineer-1", BASE, SCAN)
        self.assertIn("First paragraph.", detail.description)
        self.assertIn("Second paragraph.", detail.description)


if __name__ == "__main__":
    unittest.main()