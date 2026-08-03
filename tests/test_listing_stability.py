from __future__ import annotations

import unittest
from unittest.mock import patch

from browser import BrowserSession, PageLoadTimeoutError
from config import Config


class TogglingElement:
    """Fake web element whose visibility expires after N is_displayed calls."""

    def __init__(self, visible_calls: int | float = float("inf")):
        self.calls = 0
        self.visible_calls = visible_calls

    def is_displayed(self) -> bool:
        self.calls += 1
        return self.calls <= self.visible_calls

    def get_attribute(self, _name):
        return None


class LoadMoreButton:
    def __init__(self, on_click):
        self._on_click = on_click
        self.clicked = 0

    def is_displayed(self) -> bool:
        return True

    def click(self) -> None:
        self.clicked += 1
        self._on_click()


class StabilityFakeDriver:
    """Deterministic WebDriver fake driven by call-count schedules.

    ``route_fn(call_index)`` and ``height_fn(call_index)`` return the
    project-route count / document height for the n-th execute_script call,
    letting tests model listings that settle after a delay.
    """

    def __init__(
        self,
        *,
        route_fn=None,
        height_fn=None,
        elements: dict | None = None,
        current_url: str = "https://www.freelancermap.com/projects?sort=1",
        title: str = "Freelance projects",
        body_text: str = "Projects available",
        page_source: str = "<html><body>Projects available</body></html>",
    ):
        self.route_fn = route_fn or (lambda _index: 1)
        self.height_fn = height_fn or (lambda _index: 1000)
        self.route_probe_calls = 0
        self.height_calls = 0
        self.scroll_to_calls = 0
        self.elements = elements or {}
        self.current_url = current_url
        self.title = title
        self.body_text = body_text
        self.page_source = page_source

    def get(self, url):
        self.current_url = url

    def execute_script(self, script, *_args):
        if "document.readyState" in script:
            return "complete"
        if "innerText" in script:
            return self.body_text
        if "routes = new Set" in script or "routes.size" in script:
            self.route_probe_calls += 1
            return self.route_fn(self.route_probe_calls)
        if "scrollHeight" in script and "Math.max" in script:
            self.height_calls += 1
            return self.height_fn(self.height_calls)
        if "window.scrollTo" in script:
            self.scroll_to_calls += 1
            return None
        return None

    def find_elements(self, by, value):
        return list(self.elements.get((by, value), []))

    def quit(self):
        pass


class BrowserSessionListingStabilityTests(unittest.TestCase):
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
        self.poll_patch = patch.object(
            Config, "LISTING_STABILITY_POLL_SECONDS", 0.01, create=True
        )
        self.scroll_patch = patch.object(
            Config, "SCROLL_PAUSE_SECONDS", 0.01, create=True
        )
        self.timeout_patch = patch.object(
            Config, "PAGE_LOAD_TIMEOUT", 10, create=True
        )
        self.driver_patch = patch.object(
            BrowserSession, "_ensure_driver", lambda self: None
        )
        for item in (
            self.base_patch,
            self.http_patch,
            self.cross_patch,
            self.poll_patch,
            self.scroll_patch,
            self.timeout_patch,
            self.driver_patch,
        ):
            item.start()
            self.addCleanup(item.stop)

    def _session(self, driver) -> BrowserSession:
        session = BrowserSession(headless=True)
        session.driver = driver
        return session

    def test_listing_waits_for_two_consecutive_stable_rounds(self):
        from selenium.webdriver.common.by import By

        driver = StabilityFakeDriver()
        session = self._session(driver)
        snapshots = []
        original = session._listing_snapshot

        def recording_snapshot():
            value = original()
            snapshots.append(value)
            return value

        with patch.object(Config, "LISTING_STABLE_ROUNDS", 2):
            session._listing_snapshot = recording_snapshot
            html = session.load_listing_page(
                "https://www.freelancermap.com/projects?sort=1"
            )
        self.assertIn("Projects available", html)
        self.assertGreaterEqual(len(snapshots), 3)

    def test_delayed_route_insertion_is_awaited(self):
        """A route count that grows during polling must reset stability."""
        driver = StabilityFakeDriver(
            route_fn=lambda index: 0 if index <= 1 else 7
        )
        session = self._session(driver)
        observed = []
        original = session._listing_snapshot

        def recording_snapshot():
            value = original()
            observed.append(value[0])
            return value

        with patch.object(Config, "LISTING_STABLE_ROUNDS", 2):
            session._listing_snapshot = recording_snapshot
            html = session.load_listing_page(
                "https://www.freelancermap.com/projects?sort=1"
            )
        self.assertIn("Projects available", html)
        self.assertIn(7, observed)

    def test_scrolling_continues_while_routes_grow_with_stable_height(self):
        driver = StabilityFakeDriver(
            route_fn=lambda index: 1 if index <= 1 else 2
        )
        session = self._session(driver)
        session.scroll_to_bottom(max_scrolls=5)
        self.assertEqual(2, driver.scroll_to_calls)

    def test_scrolling_stops_when_everything_is_stable(self):
        session = self._session(StabilityFakeDriver())
        session.scroll_to_bottom(max_scrolls=5)
        self.assertEqual(1, session.driver.scroll_to_calls)

    def test_hidden_spinner_never_blocks_listing(self):
        from selenium.webdriver.common.by import By

        spinner = TogglingElement(visible_calls=0)
        driver = StabilityFakeDriver(
            elements={(By.CSS_SELECTOR, ".spinner"): [spinner]}
        )
        session = self._session(driver)
        self.assertFalse(session._visible_loading_indicator())
        html = session.load_listing_page(
            "https://www.freelancermap.com/projects?sort=1"
        )
        self.assertIn("Projects available", html)

    def test_visible_spinner_is_awaited_until_it_clears(self):
        from selenium.webdriver.common.by import By

        spinner = TogglingElement(visible_calls=3)
        driver = StabilityFakeDriver(
            elements={(By.CSS_SELECTOR, ".spinner"): [spinner]}
        )
        session = self._session(driver)
        self.assertTrue(session._wait_for_loading_finished(timeout=5))
        self.assertGreaterEqual(spinner.calls, 3)

    def test_spinner_that_never_clears_times_out_closed(self):
        from selenium.webdriver.common.by import By

        spinner = TogglingElement(visible_calls=float("inf"))
        driver = StabilityFakeDriver(
            elements={
                (By.CSS_SELECTOR, ".spinner"): [spinner],
                (
                    By.CSS_SELECTOR,
                    "a.btn-load-more, button.btn-load-more, .load-more-button, a[data-action='load-more']",
                ): [LoadMoreButton(on_click=lambda: None)],
            }
        )
        session = self._session(driver)
        with patch.object(Config, "PAGE_LOAD_TIMEOUT", 1):
            with self.assertRaises(PageLoadTimeoutError):
                session.load_listing_page(
                    "https://www.freelancermap.com/projects?sort=1"
                )

    def test_load_more_growth_is_awaited_and_clicked_up_to_limit(self):
        from selenium.webdriver.common.by import By

        state = {"routes": 1}
        button = LoadMoreButton(on_click=lambda: state.update(routes=state["routes"] + 3))
        driver = StabilityFakeDriver(
            route_fn=lambda _index: state["routes"],
            elements={
                (
                    By.CSS_SELECTOR,
                    "a.btn-load-more, button.btn-load-more, .load-more-button, a[data-action='load-more']",
                ): [button]
            },
        )
        session = self._session(driver)
        with patch.object(Config, "MAX_LOAD_MORE_CLICKS", 3):
            html = session.load_listing_page(
                "https://www.freelancermap.com/projects?sort=1"
            )
        self.assertIn("Projects available", html)
        self.assertEqual(3, button.clicked)
        self.assertEqual(10, state["routes"])

    def test_load_more_without_growth_terminates_after_one_click(self):
        from selenium.webdriver.common.by import By

        button = LoadMoreButton(on_click=lambda: None)
        driver = StabilityFakeDriver(
            elements={
                (
                    By.CSS_SELECTOR,
                    "a.btn-load-more, button.btn-load-more, .load-more-button, a[data-action='load-more']",
                ): [button]
            }
        )
        session = self._session(driver)
        with patch.object(Config, "MAX_LOAD_MORE_CLICKS", 5):
            html = session.load_listing_page(
                "https://www.freelancermap.com/projects?sort=1"
            )
        self.assertIn("Projects available", html)
        self.assertEqual(1, button.clicked)

    def test_maximum_scroll_and_click_limits_terminate_safely(self):
        from selenium.webdriver.common.by import By

        state = {"routes": 1}
        button = LoadMoreButton(on_click=lambda: state.update(routes=state["routes"] + 5))
        driver = StabilityFakeDriver(
            route_fn=lambda _index: state["routes"],
            height_fn=lambda _index: 900,
            elements={
                (
                    By.CSS_SELECTOR,
                    "a.btn-load-more, button.btn-load-more, .load-more-button, a[data-action='load-more']",
                ): [button]
            },
        )
        session = self._session(driver)
        with patch.object(Config, "MAX_LOAD_MORE_CLICKS", 2), patch.object(
            Config, "MAX_SCROLLS_PER_PAGE", 2
        ):
            html = session.load_listing_page(
                "https://www.freelancermap.com/projects?sort=1"
            )
        self.assertIn("Projects available", html)
        self.assertEqual(2, button.clicked)
        self.assertLessEqual(driver.scroll_to_calls, 2)


if __name__ == "__main__":
    unittest.main()
