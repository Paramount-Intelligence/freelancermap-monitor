from __future__ import annotations

import unittest

from parser import (
    merge_card_and_detail,
    parse_listing_cards,
    parse_project_detail,
    parse_relative_posted_time,
)

BASE = "https://www.freelancermap.com"
SCAN = "2026-07-31T12:00:00+00:00"


class AdversarialParserTests(unittest.TestCase):
    def test_01_label_followed_by_ordinary_prose_workload(self):
        html = """<main><h1>Prose Workload Test</h1>
        <div>Workload</div>
        <div>The successful candidate will lead delivery.</div></main>"""
        detail = parse_project_detail(html, BASE + "/project/prose-workload", BASE, SCAN)
        self.assertEqual("", detail.workload)

    def test_02_contract_type_followed_by_ordinary_prose(self):
        html = """<main><h1>Prose Contract Test</h1>
        <div>Contract type</div>
        <div>The role supports a global programme.</div></main>"""
        detail = parse_project_detail(html, BASE + "/project/prose-contract", BASE, SCAN)
        self.assertEqual("", detail.contract_type)

    def test_03_dom_split_location(self):
        html = """<main><h1>Split Location</h1>
        <div class="location-container"><span>Prague</span><span>, </span><span>Czech Republic</span></div></main>"""
        detail = parse_project_detail(html, BASE + "/project/split-location", BASE, SCAN)
        self.assertEqual("Prague, Czech Republic", detail.location)
        self.assertEqual("Prague", detail.city)
        self.assertEqual("Czech Republic", detail.country)

    def test_04_leading_comma_location_fragment(self):
        html = """<main><h1>Leading Comma Location</h1>
        <div class="badge-content-city">, Netherlands</div></main>"""
        detail = parse_project_detail(html, BASE + "/project/leading-comma", BASE, SCAN)
        self.assertEqual("Netherlands", detail.location)
        self.assertEqual("Netherlands", detail.city)

    def test_05_hidden_stale_detail_plus_visible_active_detail(self):
        html = """<main>
        <div class="search-result-modal hidden" aria-hidden="true">
            <h1>Stale Old Project</h1>
            <div class="project-header-info-list"><span class="badge">Berlin, Germany</span></div>
        </div>
        <div class="search-result-modal show" aria-hidden="false">
            <h1>Active Visible Project</h1>
            <div class="project-header-info-list"><span class="badge">Munich, Germany</span></div>
        </div>
        </main>"""
        detail = parse_project_detail(html, BASE + "/project/active-visible", BASE, SCAN)
        self.assertEqual("Active Visible Project", detail.title)
        self.assertEqual("Munich, Germany", detail.location)

    def test_06_active_project_plus_similar_projects(self):
        html = """<main>
        <div class="project-header"><h1>Primary Active Project</h1></div>
        <div class="project-body"><div class="project-body-description">Active project description.</div></div>
        <div data-testid="similar-projects">
            <div class="project-card"><a href="/project/similar-1">Similar Title</a><div>Duration: 99 months</div></div>
        </div>
        </main>"""
        detail = parse_project_detail(html, BASE + "/project/primary-active", BASE, SCAN)
        self.assertEqual("Primary Active Project", detail.title)
        self.assertNotIn("Similar Title", detail.description)
        self.assertNotEqual("99 months", detail.duration)

    def test_07_active_project_plus_report_modal(self):
        html = """<main>
        <div class="project-header"><h1>Report Test</h1></div>
        <div class="project-body-description">Valid project body.</div>
        <div class="modal project-header-report-modal">
            <h2>Report project</h2>
            <div>Incorrect contract type</div>
            <div>The hourly rate is unrealistic</div>
        </div>
        </main>"""
        detail = parse_project_detail(html, BASE + "/project/report-test", BASE, SCAN)
        self.assertNotIn("Report project", detail.description)
        self.assertNotIn("The hourly rate is unrealistic", detail.description)

    def test_08_active_project_plus_application_form(self):
        html = """<main>
        <div class="project-header"><h1>App Form Test</h1></div>
        <div class="project-body-description">Main body text.</div>
        <div class="banner banner-grey" data-testid="notLoggedIn">
            <h5>Log in to apply</h5>
            <div>Please log in to apply for this project.</div>
        </div>
        </main>"""
        detail = parse_project_detail(html, BASE + "/project/app-form", BASE, SCAN)
        self.assertNotIn("Log in to apply", detail.description)

    def test_09_duration_followed_by_workload_and_rate(self):
        html = """<main><h1>Duration Boundary</h1>
        <div class="project-body-description">
            Duration: 6 months initial contract Workload: Full-time Rate: €500/day
        </div></main>"""
        detail = parse_project_detail(html, BASE + "/project/dur-boundary", BASE, SCAN)
        self.assertEqual("6 months initial contract", detail.duration)
        self.assertEqual("Full-time", detail.workload)
        self.assertEqual("€500/day", detail.rate)

    def test_10_rate_containing_ir35(self):
        html = """<main><h1>IR35 Test</h1>
        <div class="project-body-description">
            Pay Rate: Competitive
            IR35 Status: Outside IR35
        </div></main>"""
        detail = parse_project_detail(html, BASE + "/project/ir35-test", BASE, SCAN)
        self.assertEqual("Competitive", detail.rate)
        self.assertNotEqual("35", detail.rate)

    def test_11_rate_with_trailing_qualifier(self):
        html = """<main><h1>Rate Qualifier</h1>
        <div class="project-body-description">
            Rate: £21.83 per hour + holidays PAYE
        </div></main>"""
        detail = parse_project_detail(html, BASE + "/project/rate-qualifier", BASE, SCAN)
        self.assertEqual("£21.83 per hour + holidays PAYE", detail.rate)

    def test_12_multiple_json_ld_job_posting_records(self):
        html = """<head><script type="application/ld+json">{"@graph":[
        {"@type":"JobPosting","url":"/project/wrong-slug","title":"Wrong Title"},
        {"@type":"JobPosting","url":"/project/right-slug","title":"Right Title","jobLocation":{"address":{"addressLocality":"Amsterdam"}}}
        ]}</script></head><main><h1>Right Title</h1></main>"""
        detail = parse_project_detail(html, BASE + "/project/right-slug", BASE, SCAN)
        self.assertEqual("Right Title", detail.title)

    def test_13_wrong_json_ld_followed_by_matching_project(self):
        html = """<head><script type="application/ld+json">[
        {"@type":"JobPosting","url":"/project/other-1","title":"Other 1"},
        {"@type":"JobPosting","url":"/project/target","title":"Target Title","description":"Matching LD body"}
        ]</script></head><main><h1>Target Title</h1></main>"""
        detail = parse_project_detail(html, BASE + "/project/target", BASE, SCAN)
        self.assertEqual("Target Title", detail.title)

    def test_14_rendered_cards_plus_stale_state_only_projects(self):
        html = """<main>
        <div class="project-card">
            <a data-testid="title" href="/project/rendered-card">Rendered Card</a>
        </div>
        <script type="application/json" data-component-name="projectSearch">{"projects":[{
            "url": "/project/stale-state-only",
            "title": "Stale State Only"
        }]}</script>
        </main>"""
        cards = parse_listing_cards(html, BASE, SCAN)
        slugs = [c.slug for c in cards]
        self.assertIn("rendered-card", slugs)
        self.assertNotIn("stale-state-only", slugs)

    def test_15_state_enrichment_matching_rendered_project(self):
        html = """<main>
        <div class="project-card">
            <a data-testid="title" href="/project/enrich-me">Enrich Me</a>
        </div>
        <script type="application/json" data-component-name="projectSearch">{"projectSearch":{"projects":[{
            "url": "/project/enrich-me",
            "title": "Enrich Me",
            "viewCount": 250
        }]}}</script>
        </main>"""
        cards = parse_listing_cards(html, BASE, SCAN)
        self.assertEqual(1, len(cards))
        self.assertEqual(250, cards[0].view_count)

    def test_16_posting_time_near_midnight(self):
        result = parse_relative_posted_time("23:55", "2026-07-31T00:10:00+00:00")
        self.assertEqual("2026-07-30T23:55:00+00:00", result)

    def test_17_publication_date_and_start_date_on_same_page(self):
        html = """<main>
        <div class="header-bar"><p>Published on 07/30/2026, 11:38 PM</p></div>
        <div class="project-header"><h1>Date Disambiguation</h1>
            <span class="badge">Start date 7/2026</span>
        </div>
        </main>"""
        detail = parse_project_detail(html, BASE + "/project/date-disambig", BASE, SCAN)
        self.assertEqual("7/2026", detail.start_date)
        self.assertEqual("2026-07-30T23:38:00+00:00", detail.posted_at)

    def test_18_login_page_supplied_to_parse_project_detail(self):
        html = """<html><head><title>Login - Freelancermap</title></head>
        <body><form id="login-form"><h1>Sign In</h1></form></body></html>"""
        detail = parse_project_detail(html, BASE + "/login", BASE, SCAN)
        self.assertEqual("", detail.title)
        self.assertEqual("", detail.description)

    def test_19_listing_page_supplied_to_parse_project_detail(self):
        html = """<html><head><title>IT Projects & Freelance Jobs</title></head>
        <body><main><h1>Find the Perfect Project</h1></main></body></html>"""
        detail = parse_project_detail(html, BASE + "/projects", BASE, SCAN)
        self.assertEqual("", detail.title)

    def test_20_access_denied_and_server_error_pages(self):
        html_denied = "<html><head><title>403 Access Denied</title></head><body><h1>Forbidden</h1></body></html>"
        detail_denied = parse_project_detail(html_denied, BASE + "/project/forbidden", BASE, SCAN)
        self.assertEqual("", detail_denied.title)

        html_error = "<html><head><title>500 Internal Server Error</title></head><body><h1>Server Error</h1></body></html>"
        detail_error = parse_project_detail(html_error, BASE + "/project/server-error", BASE, SCAN)
        self.assertEqual("", detail_error.title)

    def test_21_description_deduplication_between_card_and_detail(self):
        card_html = """<div class="project-card"><a href="/project/dedup">Dedup</a>
        <div data-testid="description"><p>Overview paragraph.</p><p>Extra card info.</p></div></div>"""
        card = parse_listing_cards(card_html, BASE, SCAN)[0]

        detail_html = """<main><h1>Dedup</h1><h2>Description</h2>
        <p>Overview paragraph.</p><p>Full detail body.</p></main>"""
        detail = parse_project_detail(detail_html, BASE + "/project/dedup", BASE, SCAN)

        merged = merge_card_and_detail(card, detail, SCAN)
        self.assertEqual(1, merged.description.count("Overview paragraph."))
        self.assertIn("Extra card info.", merged.description)
        self.assertIn("Full detail body.", merged.description)

    def test_22_empty_fields_where_source_information_does_not_exist(self):
        html = """<main><h1>Minimal Project</h1></main>"""
        detail = parse_project_detail(html, BASE + "/project/minimal", BASE, SCAN)
        self.assertEqual("Minimal Project", detail.title)
        self.assertEqual("", detail.location)
        self.assertEqual("", detail.duration)
        self.assertEqual("", detail.workload)
        self.assertEqual("", detail.rate)
        self.assertEqual("", detail.contract_type)


if __name__ == "__main__":
    unittest.main()
