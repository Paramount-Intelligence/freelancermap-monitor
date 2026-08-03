from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from browser import (
    BrowserNavigationError,
    BrowserSession,
    HttpError,
    PageState,
    _is_login_url,
)
from config import Config
from utils import exclusive_file_lock


class FakeElement:
    def __init__(
        self,
        *,
        element_id: str = "element-1",
        attributes: dict[str, str] | None = None,
        displayed: bool = True,
        enabled: bool = True,
    ) -> None:
        self.id = element_id
        self.attributes = attributes or {}
        self._displayed = displayed
        self._enabled = enabled
        self.clicked = False

    def get_attribute(self, name: str):
        return self.attributes.get(name)

    def is_displayed(self) -> bool:
        return self._displayed

    def is_enabled(self) -> bool:
        return self._enabled

    def click(self) -> None:
        self.clicked = True


class FakeSwitchTo:
    def frame(self, _frame) -> None:
        return None

    def default_content(self) -> None:
        return None


class FakeDriver:
    def __init__(
        self,
        *,
        current_url: str = "https://www.freelancermap.com/projects",
        title: str = "Freelance projects",
        body_text: str = "Projects available",
        page_source: str = "<html><body>Projects available</body></html>",
        project_routes: int = 0,
        height: int = 1000,
        elements: dict[tuple[str, str], list[FakeElement]] | None = None,
    ) -> None:
        self.current_url = current_url
        self.title = title
        self.body_text = body_text
        self.page_source = page_source
        self.project_routes = project_routes
        self.height = height
        self.elements = elements or {}
        self.window_handles = ["main"]
        self.switch_to = FakeSwitchTo()
        self.quit_called = False

    def execute_script(self, script: str, *_args):
        if "document.readyState" in script:
            return "complete"
        if "document.body ? document.body.innerText" in script:
            return self.body_text
        if "return routes.size" in script:
            return self.project_routes
        if "scrollHeight" in script and "Math.max" in script:
            return self.height
        return None

    def get(self, url: str) -> None:
        self.current_url = url

    def find_elements(self, by: str, value: str):
        return list(self.elements.get((by, value), []))

    def quit(self) -> None:
        self.quit_called = True


class BrowserSessionEnhancedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base_patch = patch.object(
            Config,
            "BASE_URL",
            "https://www.freelancermap.com",
        )
        self.http_patch = patch.object(
            Config,
            "ALLOW_INSECURE_HTTP",
            False,
            create=True,
        )
        self.cross_patch = patch.object(
            Config,
            "ALLOW_CROSS_ORIGIN_URLS",
            False,
            create=True,
        )
        self.stability_patch = patch.object(
            Config,
            "LISTING_STABILITY_POLL_SECONDS",
            0.01,
            create=True,
        )
        self.timeout_patch = patch.object(
            Config,
            "PAGE_LOAD_TIMEOUT",
            10,
            create=True,
        )
        self.driver_patch = patch.object(BrowserSession, "_ensure_driver", lambda self: setattr(self, "driver", FakeDriver()))
        self.base_patch.start()
        self.http_patch.start()
        self.cross_patch.start()
        self.stability_patch.start()
        self.timeout_patch.start()
        self.driver_patch.start()
        self.addCleanup(self.base_patch.stop)
        self.addCleanup(self.http_patch.stop)
        self.addCleanup(self.cross_patch.stop)
        self.addCleanup(self.stability_patch.stop)
        self.addCleanup(self.timeout_patch.stop)
        self.addCleanup(self.driver_patch.stop)

    def test_relative_navigation_is_same_origin_and_preserves_query(self) -> None:
        session = BrowserSession(headless=True)
        value = session._validated_navigation_url(
            "/projects?page=2#results"
        )
        self.assertEqual(
            value,
            "https://www.freelancermap.com/projects?page=2",
        )

    def test_cross_origin_navigation_is_rejected(self) -> None:
        session = BrowserSession(headless=True)
        with self.assertRaises(BrowserNavigationError):
            session._validated_navigation_url("https://example.com/project/a")

    def test_insecure_http_navigation_is_rejected(self) -> None:
        session = BrowserSession(headless=True)
        with self.assertRaises(BrowserNavigationError):
            session._validated_navigation_url(
                "http://www.freelancermap.com/projects"
            )

    def test_embedded_credentials_are_rejected(self) -> None:
        session = BrowserSession(headless=True)
        with self.assertRaises(BrowserNavigationError):
            session._validated_navigation_url(
                "https://user:password@www.freelancermap.com/projects"
            )

    def test_control_characters_are_rejected(self) -> None:
        session = BrowserSession(headless=True)
        with self.assertRaises(BrowserNavigationError):
            session._validated_navigation_url(
                "https://www.freelancermap.com/projects\nexample"
            )

    def test_same_origin_understands_default_https_port(self) -> None:
        self.assertTrue(
            BrowserSession._same_origin(
                "https://www.freelancermap.com:443/projects",
                "https://www.freelancermap.com/",
            )
        )
        self.assertFalse(
            BrowserSession._same_origin(
                "https://www.freelancermap.com:444/projects",
                "https://www.freelancermap.com/",
            )
        )

    def test_login_url_detection_uses_path_not_query(self) -> None:
        self.assertTrue(_is_login_url("https://www.freelancermap.com/login"))
        self.assertTrue(_is_login_url("https://www.freelancermap.com/sign-in/"))
        self.assertFalse(
            _is_login_url(
                "https://www.freelancermap.com/projects?return=/login"
            )
        )

    def test_project_route_count_uses_browser_dom_count(self) -> None:
        session = BrowserSession(headless=True)
        session.driver = FakeDriver(project_routes=7)  # type: ignore[assignment]
        self.assertEqual(session._project_route_count(), 7)

    def test_recaptcha_script_text_alone_is_not_a_challenge(self) -> None:
        session = BrowserSession(headless=True)
        session.driver = FakeDriver(  # type: ignore[assignment]
            page_source=(
                "<html><script src='https://google.com/recaptcha/api.js'>"
                "</script><body>Projects available</body></html>"
            ),
            body_text="Projects available",
        )
        state = session._page_state()
        self.assertIsInstance(state, PageState)
        self.assertFalse(state.challenge_detected)

    def test_visible_human_verification_is_a_challenge(self) -> None:
        session = BrowserSession(headless=True)
        session.driver = FakeDriver(  # type: ignore[assignment]
            title="Just a moment",
            body_text="Verify you are human",
        )
        self.assertTrue(session._page_state().challenge_detected)

    def test_explicit_error_page_is_detected(self) -> None:
        session = BrowserSession(headless=True)
        session.driver = FakeDriver(  # type: ignore[assignment]
            title="Service unavailable",
            body_text="Service unavailable",
        )
        self.assertTrue(session._page_state().error_detected)

    def test_next_page_url_prefers_rel_next_and_rejects_external(self) -> None:
        from selenium.webdriver.common.by import By

        next_element = FakeElement(
            element_id="next",
            attributes={
                "href": "https://www.freelancermap.com/projects?page=2"
            },
        )
        external = FakeElement(
            element_id="external",
            attributes={"href": "https://example.com/projects?page=2"},
        )
        session = BrowserSession(headless=True)
        session.driver = FakeDriver(  # type: ignore[assignment]
            elements={(By.CSS_SELECTOR, "a[rel='next']"): [external, next_element]}
        )
        self.assertEqual(
            session.next_page_url(),
            "https://www.freelancermap.com/projects?page=2",
        )

    def test_non_project_detail_url_is_rejected_before_navigation(self) -> None:
        session = BrowserSession(headless=True)
        with self.assertRaises(BrowserNavigationError):
            session.get_project_page(
                "https://www.freelancermap.com/projects"
            )

    def test_close_quits_driver_and_clears_reference(self) -> None:
        driver = FakeDriver()
        session = BrowserSession(headless=True)
        session.driver = driver  # type: ignore[assignment]
        session.close()
        self.assertTrue(driver.quit_called)
        self.assertIsNone(session.driver)

    def test_sort_state_read_from_checked_radio(self) -> None:
        from selenium.webdriver.common.by import By

        session = BrowserSession(headless=True)
        session.driver = FakeDriver(  # type: ignore[assignment]
            elements={
                (By.CSS_SELECTOR, "input[name='sort-option']:checked"): [
                    FakeElement(attributes={"value": "2"})
                ]
            }
        )
        self.assertEqual(session._current_sort_state(), "2")

    def test_listing_page_rejects_mismatched_sort_state(self) -> None:
        from selenium.webdriver.common.by import By

        session = BrowserSession(headless=True)
        session.driver = FakeDriver(  # type: ignore[assignment]
            elements={
                (By.CSS_SELECTOR, "input[name='sort-option']:checked"): [
                    FakeElement(attributes={"value": "2"})
                ]
            }
        )
        with patch.object(Config, "SCROLL_PAUSE_SECONDS", 0.01):
            with self.assertRaises(HttpError):
                session.load_listing_page(
                    "https://www.freelancermap.com/projects?sort=1",
                    expected_sort="1",
                )

    def test_listing_page_accepts_newest_sort_state(self) -> None:
        from selenium.webdriver.common.by import By

        session = BrowserSession(headless=True)
        session.driver = FakeDriver(  # type: ignore[assignment]
            elements={
                (By.CSS_SELECTOR, "input[name='sort-option']:checked"): [
                    FakeElement(attributes={"value": "1"})
                ]
            }
        )
        with patch.object(Config, "SCROLL_PAUSE_SECONDS", 0.01):
            html = session.load_listing_page(
                "https://www.freelancermap.com/projects?sort=1",
                expected_sort="1",
            )
        self.assertIn("Projects available", html)

    def test_listing_page_accepts_sort_proven_by_url(self) -> None:
        session = BrowserSession(headless=True)
        session.driver = FakeDriver()  # type: ignore[assignment]
        with patch.object(Config, "SCROLL_PAUSE_SECONDS", 0.01):
            html = session.load_listing_page(
                "https://www.freelancermap.com/projects?sort=1",
                expected_sort="1",
            )
        self.assertIn("Projects available", html)

    def test_profile_lock_released_when_chrome_startup_fails(self) -> None:
        self.driver_patch.stop()
        self.addCleanup(self.driver_patch.start)
        with tempfile.TemporaryDirectory() as folder:
            profile = Path(folder) / "chrome_profile"
            with patch.object(Config, "CHROME_PROFILE_DIR", profile), \
                 patch("browser.webdriver.Chrome", side_effect=RuntimeError("chrome exploded")):
                with self.assertRaises(RuntimeError):
                    BrowserSession(headless=True)
            lock_file = profile.parent / "chrome_profile.lock"
            self.assertTrue(lock_file.exists())
            with exclusive_file_lock(lock_file, timeout_seconds=0.5):
                pass


class FakeConsentContainer:
    """Minimal consent-banner container with clickable buttons."""

    def __init__(self, buttons, *, displayed: bool = True) -> None:
        self.buttons = buttons
        self._displayed = displayed

    def is_displayed(self) -> bool:
        return self._displayed

    def find_elements(self, by, value):
        if value == "button, a[role='button'], a":
            return self.buttons
        return []


class CookieConsentScopingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.driver_patch = patch.object(BrowserSession, "_ensure_driver", lambda self: setattr(self, "driver", FakeDriver()))
        self.driver_patch.start()
        self.addCleanup(self.driver_patch.stop)

    def _session_with(self, elements) -> BrowserSession:
        from selenium.webdriver.common.by import By

        driver = FakeDriver(
            elements={k: v for k, v in elements.items()}
        )
        session = BrowserSession(headless=True)
        session.driver = driver  # type: ignore[assignment]
        return session

    def test_unrelated_accept_buttons_are_never_queried(self) -> None:
        """Cookie acceptance must be scoped to consent containers."""
        from selenium.webdriver.common.by import By

        queries: list[str] = []
        driver = FakeDriver()
        original = driver.find_elements

        def recording(by, value):
            queries.append(value)
            return original(by, value)

        driver.find_elements = recording  # type: ignore[method-assign]
        session = BrowserSession(headless=True)
        session.driver = driver  # type: ignore[assignment]
        session.accept_cookie_banner()
        self.assertTrue(queries)
        self.assertNotIn("button[class*='accept']", queries)
        self.assertNotIn("button[id*='accept']", queries)
        self.assertNotIn("a[class*='accept']", queries)
        self.assertTrue(
            all(
                any(
                    marker in query.casefold()
                    for marker in ("cookie", "consent", "onetrust", "gdpr", "cc-banner")
                )
                for query in queries
            ),
            msg=f"unscoped selectors queried: {queries}",
        )

    def test_consent_banner_accept_button_is_clicked_once(self) -> None:
        from selenium.webdriver.common.by import By

        accept = FakeElement(
            element_id="accept-all",
            attributes={"class": "cookie-consent-accept"},
        )
        accept.text = "Accept all cookies"
        decline = FakeElement(element_id="decline")
        decline.text = "Decline"
        session = self._session_with(
            {
                (By.CSS_SELECTOR, "[class*='cookie-consent']"): [
                    FakeConsentContainer([accept, decline])
                ]
            }
        )
        session.accept_cookie_banner()
        self.assertTrue(accept.clicked)
        self.assertFalse(decline.clicked)

    def test_hidden_consent_banner_is_never_clicked(self) -> None:
        from selenium.webdriver.common.by import By

        accept = FakeElement(element_id="accept-all")
        accept.text = "Accept all cookies"
        session = self._session_with(
            {
                (By.CSS_SELECTOR, "[class*='cookie-consent']"): [
                    FakeConsentContainer([accept], displayed=False)
                ]
            }
        )
        session.accept_cookie_banner()
        self.assertFalse(accept.clicked)


if __name__ == "__main__":
    unittest.main()