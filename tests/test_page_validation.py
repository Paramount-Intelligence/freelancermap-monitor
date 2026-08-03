from __future__ import annotations

import unittest
from unittest.mock import patch

from browser import (
    BrowserNavigationError,
    BrowserSession,
    HttpError,
    PageLoadTimeoutError,
    PageNotFoundError,
)
from config import Config


class FakeElement:
    def __init__(self, *, attributes=None, displayed=True):
        self.attributes = attributes or {}
        self._displayed = displayed
        self.clicked = False

    def get_attribute(self, name):
        return self.attributes.get(name)

    def is_displayed(self):
        return self._displayed

    def is_enabled(self):
        return True

    def click(self):
        self.clicked = True


class PageValidationFakeDriver:
    """Configurable WebDriver fake for the post-navigation boundary.

    ``get()`` copies the requested URL by default; ``forced_url`` lets a
    test simulate server-side redirects and ``strip_query_on_get`` simulates
    navigations that drop the query string. ``ready_state`` and
    ``script_error`` drive the page-load timeout paths.
    """

    def __init__(
        self,
        *,
        ready_state: str = "complete",
        script_error: bool = False,
        forced_url: str | None = None,
        strip_query_on_get: bool = False,
        title: str = "Freelance projects",
        body_text: str = "Projects available",
        page_source: str = "<html><body>Projects available</body></html>",
        elements: dict | None = None,
    ):
        self.ready_state = ready_state
        self.script_error = script_error
        self.forced_url = forced_url
        self.strip_query_on_get = strip_query_on_get
        self.title = title
        self.body_text = body_text
        self.page_source = page_source
        self.elements = elements or {}
        self.current_url = "https://www.freelancermap.com/projects"

    def get(self, url):
        if self.forced_url is not None:
            self.current_url = self.forced_url
        elif self.strip_query_on_get:
            self.current_url = url.split("?", 1)[0]
        else:
            self.current_url = url

    def execute_script(self, script, *_args):
        if self.script_error:
            raise RuntimeError("execute_script exploded")
        if "document.readyState" in script:
            return self.ready_state
        if "innerText" in script:
            return self.body_text
        if "routes.size" in script:
            return 0
        if "scrollHeight" in script and "Math.max" in script:
            return 1000
        return None

    def find_elements(self, by, value):
        return list(self.elements.get((by, value), []))

    def quit(self):
        pass


class BrowserSessionPageValidationTests(unittest.TestCase):
    def setUp(self):
        self.base_patch = patch.object(
            Config, "BASE_URL", "https://www.freelancermap.com"
        )
        self.http_patch = patch.object(
            Config, "ALLOW_INSECURE_HTTP", False, create=True
        )
        self.cross_patch = patch.object(
            Config, "ALLOW_CROSS_ORIGIN_URLS", False, create=True
        )
        self.stability_patch = patch.object(
            Config, "LISTING_STABILITY_POLL_SECONDS", 0.01, create=True
        )
        self.timeout_patch = patch.object(Config, "PAGE_LOAD_TIMEOUT", 10, create=True)
        self.scroll_patch = patch.object(Config, "SCROLL_PAUSE_SECONDS", 0.01, create=True)
        self.driver_patch = patch.object(
            BrowserSession, "_ensure_driver", lambda self: None
        )
        for item in (
            self.base_patch,
            self.http_patch,
            self.cross_patch,
            self.stability_patch,
            self.timeout_patch,
            self.scroll_patch,
            self.driver_patch,
        ):
            item.start()
            self.addCleanup(item.stop)

    def _session(self, driver) -> BrowserSession:
        session = BrowserSession(headless=True)
        session.driver = driver
        return session

    def _driver(self, **kwargs) -> PageValidationFakeDriver:
        return PageValidationFakeDriver(**kwargs)


class PageLoadTimeoutTests(BrowserSessionPageValidationTests):
    def test_ready_state_complete_page_loads_without_exception(self):
        session = self._session(self._driver())
        html = session.load_listing_page(
            "https://www.freelancermap.com/projects?sort=1"
        )
        self.assertIn("Projects available", html)

    def test_loading_ready_state_times_out_closed(self):
        session = self._session(
            self._driver(ready_state="loading")
        )
        with patch.object(Config, "PAGE_LOAD_TIMEOUT", 1):
            with self.assertRaises(PageLoadTimeoutError) as ctx:
                session.load_listing_page(
                    "https://www.freelancermap.com/projects?sort=1"
                )
        self.assertIn("https://www.freelancermap.com/projects", str(ctx.exception))
        self.assertNotIn("Projects available", str(ctx.exception))

    def test_execute_script_failure_times_out_closed(self):
        session = self._session(self._driver(script_error=True))
        with patch.object(Config, "PAGE_LOAD_TIMEOUT", 1):
            with self.assertRaises(PageLoadTimeoutError):
                session.load_listing_page(
                    "https://www.freelancermap.com/projects?sort=1"
                )

    def test_timeout_propagates_from_detail_navigation(self):
        session = self._session(self._driver(ready_state="loading"))
        with patch.object(Config, "PAGE_LOAD_TIMEOUT", 1):
            with self.assertRaises(PageLoadTimeoutError):
                session.get_project_page(
                    "https://www.freelancermap.com/project/example"
                )

    def test_timeout_propagates_from_account_verification(self):
        session = self._session(self._driver(ready_state="loading"))
        with patch.object(Config, "PAGE_LOAD_TIMEOUT", 1):
            result = session.verify_authenticated_session()
        self.assertFalse(result.authenticated)
        self.assertIn("ready state", result.reason.casefold())

    def test_timeout_propagates_from_generic_navigation(self):
        session = self._session(self._driver(ready_state="loading"))
        with patch.object(Config, "PAGE_LOAD_TIMEOUT", 1):
            with self.assertRaises(PageLoadTimeoutError):
                session.navigate("https://www.freelancermap.com/projects")


class ListingSortFailClosedTests(BrowserSessionPageValidationTests):
    def test_sort_proven_by_final_url_passes(self):
        session = self._session(self._driver())
        html = session.load_listing_page(
            "https://www.freelancermap.com/projects?sort=1",
            expected_sort="1",
        )
        self.assertIn("Projects available", html)

    def test_sort_proven_by_dom_when_url_missing_passes(self):
        from selenium.webdriver.common.by import By

        driver = self._driver(strip_query_on_get=True)
        driver.elements = {
            (By.CSS_SELECTOR, "input[name='sort-option']:checked"): [
                FakeElement(attributes={"value": "1"})
            ]
        }
        session = self._session(driver)
        html = session.load_listing_page(
            "https://www.freelancermap.com/projects?sort=1",
            expected_sort="1",
        )
        self.assertIn("Projects available", html)

    def test_sort_fails_closed_when_url_and_dom_proof_missing(self):
        session = self._session(self._driver(strip_query_on_get=True))
        with self.assertRaises(HttpError) as ctx:
            session.load_listing_page(
                "https://www.freelancermap.com/projects?sort=1",
                expected_sort="1",
            )
        self.assertIn("unverifiable", str(ctx.exception).casefold())

    def test_sort_fails_closed_on_dom_conflict(self):
        from selenium.webdriver.common.by import By

        driver = self._driver()
        driver.elements = {
            (By.CSS_SELECTOR, "input[name='sort-option']:checked"): [
                FakeElement(attributes={"value": "2"})
            ]
        }
        session = self._session(driver)
        with self.assertRaises(HttpError) as ctx:
            session.load_listing_page(
                "https://www.freelancermap.com/projects?sort=1",
                expected_sort="1",
            )
        self.assertIn("expected", str(ctx.exception).casefold())

    def test_sort_fails_closed_on_url_conflict(self):
        driver = self._driver(
            forced_url="https://www.freelancermap.com/projects?sort=2"
        )
        session = self._session(driver)
        with self.assertRaises(HttpError) as ctx:
            session.load_listing_page(
                "https://www.freelancermap.com/projects?sort=1",
                expected_sort="1",
            )
        self.assertIn("differently sorted", str(ctx.exception).casefold())

    def test_secondary_feed_sort_two_accepted_without_expected_sort(self):
        driver = self._driver(
            forced_url="https://www.freelancermap.com/projects?sort=2"
        )
        session = self._session(driver)
        html = session.load_listing_page(
            "https://www.freelancermap.com/projects?sort=2"
        )
        self.assertIn("Projects available", html)


class SharedPostNavigationBoundaryTests(BrowserSessionPageValidationTests):
    def test_boundary_rejects_error_page_after_navigation(self):
        session = self._session(
            self._driver(title="404 Not Found", body_text="Page not found")
        )
        with self.assertRaises(Exception):
            session.load_listing_page("https://www.freelancermap.com/projects?sort=1")

    def test_boundary_rejects_login_redirect_after_navigation(self):
        session = self._session(
            self._driver(forced_url="https://www.freelancermap.com/login")
        )
        with self.assertRaises(HttpError) as ctx:
            session.load_listing_page("https://www.freelancermap.com/projects?sort=1")
        self.assertIn("login", str(ctx.exception).casefold())

    def test_boundary_rejects_password_form_after_navigation(self):
        from selenium.webdriver.common.by import By

        driver = self._driver()
        driver.elements = {
            (By.CSS_SELECTOR, "input[type='password']"): [
                FakeElement(displayed=True)
            ]
        }
        session = self._session(driver)
        with self.assertRaises(HttpError) as ctx:
            session.load_listing_page("https://www.freelancermap.com/projects?sort=1")
        self.assertIn("password form", str(ctx.exception).casefold())

    def test_boundary_rejects_challenge_after_navigation(self):
        session = self._session(
            self._driver(
                title="Just a moment",
                body_text="Verify you are human",
            )
        )
        with self.assertRaises((HttpError, PageNotFoundError)) as ctx:
            session.load_listing_page("https://www.freelancermap.com/projects?sort=1")
        self.assertIn("404", str(ctx.exception))

    def test_boundary_rejects_empty_body_after_navigation(self):
        session = self._session(self._driver(body_text=""))
        with self.assertRaises(HttpError) as ctx:
            session.load_listing_page("https://www.freelancermap.com/projects?sort=1")
        self.assertIn("empty", str(ctx.exception).casefold())

    def test_boundary_rejects_non_https_final_url(self):
        session = self._session(
            self._driver(
                forced_url="http://www.freelancermap.com/projects?sort=1"
            )
        )
        with self.assertRaises(BrowserNavigationError):
            session.load_listing_page("https://www.freelancermap.com/projects?sort=1")

    def test_boundary_rejects_cross_origin_final_url(self):
        session = self._session(
            self._driver(
                forced_url="https://evil.example.com/projects?sort=1"
            )
        )
        with self.assertRaises(BrowserNavigationError):
            session.load_listing_page("https://www.freelancermap.com/projects?sort=1")

    def test_boundary_rejects_embedded_credentials_in_final_url(self):
        session = self._session(
            self._driver(
                forced_url="https://user:password@www.freelancermap.com/projects?sort=1"
            )
        )
        with self.assertRaises(BrowserNavigationError):
            session.load_listing_page("https://www.freelancermap.com/projects?sort=1")

    def test_boundary_rejects_wrong_route_type_for_listing(self):
        session = self._session(
            self._driver(forced_url="https://www.freelancermap.com/my_account.html")
        )
        with self.assertRaises(BrowserNavigationError) as ctx:
            session.load_listing_page("https://www.freelancermap.com/projects?sort=1")
        self.assertIn("listing/search route", str(ctx.exception))

    def test_detail_boundary_accepts_matching_project_key(self):
        session = self._session(self._driver())
        html = session.get_project_page(
            "https://www.freelancermap.com/project/example"
        )
        self.assertIn("Projects available", html)

    def test_detail_boundary_rejects_mismatched_project_key(self):
        session = self._session(
            self._driver(
                forced_url="https://www.freelancermap.com/project/other-project"
            )
        )
        with self.assertRaises(HttpError) as ctx:
            session.get_project_page(
                "https://www.freelancermap.com/project/example"
            )
        self.assertIn("does not match", str(ctx.exception).casefold())

    def test_generic_navigation_passes_boundary(self):
        session = self._session(self._driver())
        session.navigate("https://www.freelancermap.com/projects")
        self.assertEqual(
            "https://www.freelancermap.com/projects",
            session.driver.current_url,
        )


if __name__ == "__main__":
    unittest.main()
