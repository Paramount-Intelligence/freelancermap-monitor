from __future__ import annotations

import unittest
from unittest.mock import patch

from browser import (
    BrowserNavigationError,
    BrowserSession,
    PageState,
    _is_login_url,
)
from config import Config


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
        self.base_patch.start()
        self.http_patch.start()
        self.cross_patch.start()
        self.addCleanup(self.base_patch.stop)
        self.addCleanup(self.http_patch.stop)
        self.addCleanup(self.cross_patch.stop)

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


if __name__ == "__main__":
    unittest.main()