from __future__ import annotations

import unittest

import pagedetect
from browser import ERROR_BODY_RE, ERROR_TITLE_RE, _is_login_url
from monitor import DetailValidationError, validate_detail
from pagedetect import (
    detect_challenge,
    detect_error,
    has_error_context,
    has_error_title,
    is_generic_error_title,
    is_login_url,
)
from parser import ProjectDetail, parse_project_detail


BASE = "https://www.freelancermap.com"


def detail_html(title_text: str, body: str = "<p>Body</p>") -> str:
    return f"<html><head><title>{title_text}</title></head><body><main><h1>{title_text}</h1>{body}</main></body></html>"


class GenericTitleExactMatchTests(unittest.TestCase):
    """Bare numbers and generic words inside real titles must never reject."""

    LEGIT_TITLES = (
        "Error Handling Engineer",
        "Login Security Specialist",
        "Access Management Consultant",
        "Server Error Monitoring Developer",
        "Fortune 500 Data Architect",
        "Forbidden API Migration Specialist",
        "Not Found Handler Developer",
        "Senior Python Developer - £500/day",
        "IT Projects Manager",
        "Freelance Jobs Coordinator",
    )

    def test_real_project_titles_are_never_rejected(self) -> None:
        for title in self.LEGIT_TITLES:
            self.assertFalse(has_error_title(title), f"false positive: {title}")
            self.assertFalse(is_generic_error_title(title))

    def test_real_project_titles_pass_monitor_validation(self) -> None:
        for title in self.LEGIT_TITLES:
            detail = ProjectDetail(
                source_key="example",
                slug="example",
                title=title,
                description="Real project description.",
                company="Acme GmbH",
                location="Berlin",
                url=f"{BASE}/project/example",
            )
            validate_detail(detail)  # must not raise

    def test_real_project_titles_survive_parser(self) -> None:
        for title in self.LEGIT_TITLES:
            detail = parse_project_detail(
                detail_html(title), f"{BASE}/project/{title.lower().replace(' ', '-')}", BASE
            )
            self.assertEqual(title, detail.title)


class ErrorTitleRejectionTests(unittest.TestCase):
    GENERIC_TITLES = (
        "404 Not Found",
        "410 Gone",
        "429 Too Many Requests",
        "500 Internal Server Error",
        "502 Bad Gateway",
        "503 Service Unavailable",
        "Error",
        "Login",
        "Sign in",
        "Log in",
        "Access denied",
        "Forbidden",
        "Page not found",
        "Bad Gateway",
        "Server Error",
        "Service unavailable",
        "Verify you are human",
        "Just a moment",
        "Find the perfect project",
        "IT Projects & Freelance Jobs",
        "Freelance Jobs & IT Projects Worldwide | freelancermap",
    )

    def test_generic_error_titles_are_rejected(self) -> None:
        for title in self.GENERIC_TITLES:
            self.assertTrue(has_error_title(title), f"missed: {title}")

    def test_contextual_phrases_are_rejected(self) -> None:
        for text in (
            "404 Not Found",
            "Page Not Found",
            "HTTP 429 Too Many Requests",
            "Page 404 - Not Found",
            "500 Internal Server Error",
            "410 Resource is Gone",
            "Error 500",
            "403 Access Denied",
            "Something went wrong, please try again later",
        ):
            self.assertTrue(has_error_context(text), f"missed: {text}")

    def test_bare_status_numbers_are_never_error_context(self) -> None:
        for text in (
            "Fortune 500 Data Architect",
            "Rate: $500.00 per day",
            "€404,000 budget per year",
            "Onboarding fee 1.500 EUR",
            "SC Cleared SOC Analyst - £500/day via Umbrella",
            "We need 500 engineers for the platform",
            "Error Handling Engineer",
            "status 200 OK",
        ):
            self.assertFalse(has_error_context(text), f"false positive: {text}")

    def test_monitor_validation_rejects_error_titles(self) -> None:
        for title in ("404 Not Found", "Login", "Server Error", "Access denied"):
            detail = ProjectDetail(
                source_key="example",
                slug="example",
                title=title,
                description="ignored",
                url=f"{BASE}/project/example",
            )
            with self.assertRaises(DetailValidationError):
                validate_detail(detail)


class ChallengeDetectionTests(unittest.TestCase):
    def test_two_explicit_phrases_are_a_challenge(self) -> None:
        self.assertTrue(detect_challenge("Just a moment", "Verify you are human", ""))

    def test_explicit_phrase_plus_marker_is_a_challenge(self) -> None:
        self.assertTrue(detect_challenge("Just a moment", "Please wait", ""))
        self.assertTrue(detect_challenge("", "Verify you are human, security check", ""))

    def test_single_please_wait_is_never_a_challenge(self) -> None:
        self.assertFalse(detect_challenge("", "Please wait", ""))
        self.assertFalse(detect_challenge("Projects", "Please wait while we load", ""))

    def test_recaptcha_script_tag_alone_is_not_a_challenge(self) -> None:
        source = "<html><script src='https://google.com/recaptcha/api.js'></script><body>Projects</body></html>"
        self.assertFalse(detect_challenge("", "Projects available", source))

    def test_visible_challenge_iframe_is_a_challenge(self) -> None:
        source = "<iframe src='https://www.google.com/recaptcha/api2/anchor'></iframe>"
        self.assertTrue(detect_challenge("", "", source))

    def test_one_time_code_form_is_a_challenge(self) -> None:
        source = "<input type='text' autocomplete='one-time-code'/>"
        self.assertTrue(detect_challenge("", "", source))

    def test_recaptcha_widget_on_content_rich_page_is_not_a_challenge(self) -> None:
        source = (
            "<iframe title='reCAPTCHA' width='304' height='78' "
            "src='https://www.google.com/recaptcha/api2/anchor'></iframe>"
        )
        body = (
            "Published on 08/03/2026 IT Integration Analyst EU Remote 6 months "
            "Mazowieckie Poland On-site Freelance. We are looking for an "
            "Integration Analyst to join our enterprise platform team for a "
            "six-month assignment. The successful candidate will support data "
            "integration, API orchestration, and incident triage across the "
            "platform landscape."
        )
        self.assertFalse(
            detect_challenge(
                "Integration Analyst - EU Remote - 6 months on www.freelancermap.com",
                body,
                source,
            )
        )

    def test_turnstile_widget_on_content_rich_page_is_not_a_challenge(self) -> None:
        source = (
            "<iframe src='https://challenges.cloudflare.com/turnstile/v0/abc/widget'"
            " width='300' height='65'></iframe>"
        )
        body = "A" * 500  # well above the content-thin threshold
        self.assertFalse(detect_challenge("Senior Python Developer", body, source))

    def test_cloudflare_interstitial_iframe_is_definitive(self) -> None:
        source = (
            "<iframe src='https://challenges.cloudflare.com/cdn-cgi/challenge-platform/"
            "h/g/cv/result'></iframe>"
        )
        self.assertTrue(detect_challenge("", "", source))

    def test_widget_iframe_plus_interstitial_marker_is_a_challenge(self) -> None:
        source = (
            "<iframe src='https://www.google.com/recaptcha/api2/anchor'></iframe>"
        )
        self.assertTrue(
            detect_challenge("Projects", "Checking your browser before proceeding", source)
        )

    def test_widget_iframe_with_thin_body_is_a_challenge(self) -> None:
        source = (
            "<iframe src='https://www.google.com/recaptcha/api2/anchor'></iframe>"
        )
        self.assertTrue(detect_challenge("", "short page", source))


class SharedSemanticsTests(unittest.TestCase):
    def test_browser_regexes_are_the_shared_pattern(self) -> None:
        self.assertIs(ERROR_TITLE_RE, pagedetect.ERROR_TITLE_RE)
        self.assertIs(ERROR_BODY_RE, pagedetect.ERROR_BODY_RE)
        self.assertIs(ERROR_TITLE_RE, ERROR_BODY_RE)

    def test_browser_title_and_body_share_one_rule(self) -> None:
        self.assertTrue(ERROR_TITLE_RE.search("404 Not Found"))
        self.assertFalse(ERROR_TITLE_RE.search("Fortune 500 Data Architect"))

    def test_detect_error_uses_title_then_body(self) -> None:
        self.assertTrue(detect_error("404 Not Found", "Projects available"))
        self.assertTrue(detect_error("Senior Python Developer", "Page 404 - Not Found"))
        self.assertFalse(detect_error("Senior Python Developer", "Projects available"))

    def test_login_url_detection(self) -> None:
        self.assertTrue(is_login_url("https://www.freelancermap.com/login"))
        self.assertTrue(is_login_url("https://www.freelancermap.com/sign-in/"))
        self.assertFalse(is_login_url("https://www.freelancermap.com/projects?return=/login"))
        self.assertIs(is_login_url, _is_login_url)


if __name__ == "__main__":
    unittest.main()
