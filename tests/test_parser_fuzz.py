from __future__ import annotations

import random
import re
import unittest
from datetime import datetime, timezone

from parser import parse_listing_cards, parse_project_detail

BASE = "https://www.freelancermap.com"
SCAN = "2026-07-31T12:00:00+00:00"

TAGS = ["p", "div", "span", "h1", "h2", "h3", "li", "ul", "section", "article", "main", "script", "form", "button"]
LABELS = [
    "Location", "Workload", "Duration", "Start Date", "Rate", "Contract Type", "Workplace",
    "Description", "Report project", "Similar projects", "Reason for reporting this project:",
]
VALUES = [
    "The", "The successful candidate will lead delivery.", "IR35 Status: Outside IR35",
    "Pay Rate: Competitive", "£350 a day (Inside IR35)", ", Netherlands", "Rozenburg \n , \n Netherlands",
    "6 months initial contract Workload: Full-time", "12 months (extension possible for 1 year)",
    "80% remote", "100% workload", "Published on 07/30/2026, 11:38 PM", "21:03", "2026-08-15T00:00:00+00:00",
    "http://external-site.com/project/fake", "/project/valid-slug", "http://evil.com", "\x00\x01\x02",
    "<b>Broken tags", "</html></body>", "{\"@type\":\"JobPosting\",\"url\":\"/project/fuzz\"}",
]


class ParserFuzzTests(unittest.TestCase):
    def test_1000_fuzz_inputs_and_invariants(self):
        rng = random.Random(42)

        for iteration in range(1000):
            # Construct a randomized HTML string
            num_elements = rng.randint(1, 15)
            fragments = ["<main>"]
            for _ in range(num_elements):
                tag = rng.choice(TAGS)
                label = rng.choice(LABELS)
                val = rng.choice(VALUES)
                cls = f'class="{rng.choice(["project-card", "modal", "hidden", "project-body-description", "badge"])}"'
                attr = f'data-testid="{rng.choice(["title", "city", "created", "duration", "type", "description"])}"'
                
                # Introduce intentional broken nesting / tags randomly
                if rng.random() < 0.1:
                    fragments.append(f"<{tag} {cls}> {label}: {val}")  # unclosed tag
                else:
                    fragments.append(f"<{tag} {cls} {attr}> {label}: {val} </{tag}>")

            fragments.append("</main>")
            html = "\n".join(fragments)

            # Invariant 1: Parser never crashes on parse_project_detail
            try:
                detail = parse_project_detail(html, BASE + f"/project/fuzz-{iteration}", BASE, SCAN)
            except Exception as exc:
                self.fail(f"parse_project_detail crashed on iteration {iteration}: {exc}\nHTML:\n{html}")

            # Invariant 1b: Parser never crashes on parse_listing_cards
            try:
                cards = parse_listing_cards(html, BASE, SCAN)
            except Exception as exc:
                self.fail(f"parse_listing_cards crashed on iteration {iteration}: {exc}\nHTML:\n{html}")

            # Validate fields on detail
            # Invariant 2: URL remains same-origin
            if detail.url:
                self.assertTrue(detail.url.startswith("https://www.freelancermap.com"))

            # Invariant 3: No field equals only "The"
            for f_name in ("title", "location", "duration", "workload", "rate", "contract_type", "workplace"):
                val = getattr(detail, f_name)
                self.assertNotEqual("The", val, f"Iteration {iteration}: field {f_name} equaled 'The'")

            # Invariant 4: Location does not begin with comma
            self.assertFalse(detail.location.startswith(","), f"Iteration {iteration}: location started with comma: {detail.location!r}")

            # Invariant 5: Rate does not become "35" from IR35
            self.assertNotEqual("35", detail.rate, f"Iteration {iteration}: rate became '35'")

            # Invariant 6: Duration stays below a reasonable maximum length
            self.assertLess(len(detail.duration), 150, f"Iteration {iteration}: duration too long: {detail.duration!r}")

            # Invariant 7: posted_at is a valid UTC ISO-8601 string
            try:
                dt_posted = datetime.fromisoformat(detail.posted_at)
            except ValueError:
                self.fail(f"Iteration {iteration}: invalid posted_at format {detail.posted_at!r}")

            # Invariant 8: posted_at is not later than scan_at beyond small tolerance
            dt_scan = datetime.fromisoformat(SCAN)
            self.assertLessEqual(
                dt_posted,
                dt_scan,
                f"Iteration {iteration}: posted_at {dt_posted} is in future relative to scan_at {dt_scan}"
            )


if __name__ == "__main__":
    unittest.main()
