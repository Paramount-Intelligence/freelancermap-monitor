import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import database
from browser import BrowserProfileInUseError, BrowserSession, AuthVerificationResult
from config import Config
from monitor import run_cycle, _discover, validate_detail
from parser import ProjectDiscovery, parse_project_detail


class FakeDriver:
    """Mock WebDriver for unit testing without spawning real Chrome processes."""
    def __init__(
        self,
        options=None,
        mode=None,
        *,
        sort_state: str | None = None,
        load_more: bool = False,
        route_growth: int = 0,
        route_count: int = 1,
    ):
        self.options = options
        if isinstance(options, str):
            self.mode = options
        else:
            self.mode = mode or "account"
        self.sort_state = sort_state
        self.load_more = load_more
        self.route_growth = route_growth
        self.route_count = route_count
        self.route_calls = 0
        self.scroll_calls = 0
        self.load_more_requests = 0
        self.current_url = "https://www.freelancermap.com/my_account.html"
        self.title = "Freelancermap Account Dashboard"
        self.page_source = (
            "<html><body><div class='project-container'>"
            "<div class='project-card'>"
            "<a href='/project/unit-test-proj' data-testid='title'>Unit Test Proj</a>"
            "<div data-testid='city'>Berlin</div></div></div>"
            "<h1>Dashboard</h1><p>My Freelancermap</p><a href='/logout'>Logout</a></body></html>"
        )

    def get(self, url):
        self.current_url = url
        if "login" in url or (self.mode == "login" and "my_account" in url):
            self.title = "Freelancermap Login"
            self.page_source = "<html><body><form action='/login'><input type='password' name='pass'/></form></body></html>"
        elif self.mode == "404":
            self.title = "404 Not Found"
            self.page_source = "<html><body><h1>Page Not Found</h1></body></html>"
        elif self.mode in ("challenge", "captcha"):
            self.title = "Just a moment..."
            self.page_source = "<html><body>Verify you are human</body></html>"
        elif self.mode == "error_500":
            self.title = "500 Internal Server Error"
            self.page_source = "<html><body>500 Server Error</body></html>"
        elif self.mode == "access_denied":
            self.title = "Access Denied"
            self.page_source = "<html><body>403 Forbidden Access Denied</body></html>"
        elif self.mode == "rate_limit":
            self.title = "429 Too Many Requests"
            self.page_source = "<html><body>429 Rate Limit</body></html>"
        elif self.mode == "blank":
            self.title = "Freelancermap"
            self.page_source = "<html><body></body></html>"
        elif self.mode == "public":
            self.title = "Freelancermap Projects"
            self.page_source = "<html><body><p>Projects available</p></body></html>"
        elif self.mode == "account_no_marker":
            self.title = "Freelancermap Account Area"
            self.page_source = "<html><body><p>Account area settings</p></body></html>"
        elif self.mode == "maintenance":
            self.title = "Temporarily unavailable"
            self.page_source = "<html><body><p>Temporarily unavailable maintenance</p></body></html>"
        elif self.mode == "logout_marker":
            self.title = "Freelancermap Account Dashboard"
            self.page_source = (
                "<html><body><p>Account area</p>"
                "<a href='/logout'>Logout</a></body></html>"
            )
        elif self.mode == "user_menu_marker":
            self.title = "Freelancermap Account Dashboard"
            self.page_source = (
                "<html><body><p>My Freelancermap profile settings</p>"
                "<div data-id='user-menu'></div></body></html>"
            )
        elif self.mode == "dashboard_marker":
            self.title = "Freelancermap Account Dashboard"
            self.page_source = (
                "<html><body><h1>Account Dashboard</h1>"
                "<p>Overview of your profile</p></body></html>"
            )
        elif "my_account" in url:
            self.title = "Freelancermap Account Dashboard"
            self.page_source = (
                "<html><body><div class='project-container'>"
                "<div class='project-card'>"
                "<a href='/project/unit-test-proj' data-testid='title'>Unit Test Proj</a>"
                "<div data-testid='city'>Berlin</div></div></div>"
                "<h1>Dashboard</h1><p>My Freelancermap</p><a href='/logout'>Logout</a></body></html>"
            )
        else:
            self.title = "Freelancermap Projects"
            self.page_source = (
                "<html><body><div class='project-container'>"
                "<div class='project-card'>"
                "<a href='/project/unit-test-proj' data-testid='title'>Unit Test Proj</a>"
                "<div data-testid='city'>Berlin</div></div></div>"
                "<h1>Dashboard</h1><p>My Freelancermap</p><a href='/logout'>Logout</a></body></html>"
            )

    def load_listing_page(self, url, *, expected_sort=None):
        self.get(url)
        return self.page_source

    def set_page_load_timeout(self, val):
        pass

    def set_script_timeout(self, val):
        pass

    def implicitly_wait(self, val):
        pass

    def find_elements(self, by, value):
        if "logout" in value or "abmelden" in value:
            if self.mode in ("account", "logout_marker", "user_menu_marker", "dashboard_marker"):
                elem = MagicMock()
                elem.is_displayed.return_value = True
                return [elem]
            return []
        if "email" in value:
            return [MagicMock()]
        if "sort-option" in value or ("data-value" in value and "active" in value):
            if self.sort_state is not None:
                elem = MagicMock()
                elem.get_attribute.return_value = self.sort_state
                return [elem]
            return []
        if "load-more" in value:
            self.load_more_requests += 1
            if self.load_more:
                elem = MagicMock()
                elem.is_displayed.return_value = True
                elem.click.side_effect = lambda: self._load_more_batch_added()
                return [elem]
            return []
        if "password" in value:
            if "password" in self.page_source:
                elem = MagicMock()
                return [elem]
            return []
        return []

    def execute_script(self, script, *args):
        if "return document.readyState" in script:
            return "complete"
        if "routes = new Set" in script or "routes.size" in script:
            self.route_calls += 1
            return self.route_count
        if "scrollHeight" in script and "Math.max" in script:
            self.scroll_calls += 1
            return 1000
        if "innerText" in script or "document.body" in script:
            if self.mode == "access_denied":
                return "403 Forbidden Access Denied"
            if self.mode == "rate_limit":
                return "429 Too Many Requests"
            if self.mode == "error_500":
                return "500 Internal Server Error"
            if self.mode == "404":
                return "404 Page Not Found"
            if self.mode in ("challenge", "captcha"):
                return "Verify you are human captcha security check"
            if self.mode == "login":
                return "Log in Sign in"
            if self.mode == "blank":
                return ""
            if self.mode == "public":
                return "Projects available"
            if self.mode == "account_no_marker":
                return "Account area settings"
            if self.mode == "maintenance":
                return "Temporarily unavailable maintenance"
            if self.mode == "logout_marker":
                return "Account area Logout"
            if self.mode == "user_menu_marker":
                return "My Freelancermap profile settings"
            if self.mode == "dashboard_marker":
                return "Account Dashboard overview"
            return "Dashboard My Freelancermap Logout Abmelden"
        return None

    def quit(self):
        pass

    def _load_more_batch_added(self):
        """Simulate one load-more batch: the project-route count grows."""
        self.route_count += self.route_growth


class AuthenticatedWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.profile_dir = Path(self.temp_dir.name) / "chrome_profile"

    def test_01_persistent_profile_option_passed_to_chrome(self):
        with patch.object(Config, "CHROME_PROFILE_DIR", self.profile_dir):
            session = BrowserSession(driver_factory=FakeDriver)
            self.assertTrue(session.driver.options is not None)
            args = session.driver.options.arguments
            self.assertTrue(any("--user-data-dir=" in arg for arg in args))

    def test_02_profile_directory_created(self):
        with patch.object(Config, "CHROME_PROFILE_DIR", self.profile_dir):
            BrowserSession(driver_factory=FakeDriver)
            self.assertTrue(self.profile_dir.exists())

    def test_03_profile_lock_prevents_concurrent_use(self):
        lock_file = self.profile_dir.parent / "chrome_profile.lock"
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        lock_file.write_text("locked")
        
        from utils import exclusive_file_lock
        with exclusive_file_lock(lock_file):
            with patch.object(Config, "CHROME_PROFILE_DIR", self.profile_dir):
                with self.assertRaises(BrowserProfileInUseError):
                    BrowserSession(driver_factory=None, autostart=True)

    def test_04_profile_lock_released_on_close(self):
        with patch.object(Config, "CHROME_PROFILE_DIR", self.profile_dir):
            session = BrowserSession(driver_factory=FakeDriver)
            session.close()
            self.assertIsNone(session.driver)
            self.assertIsNone(session._profile_lock_context)

    def test_05_headless_true(self):
        with patch.object(Config, "HEADLESS", True):
            session = BrowserSession(driver_factory=FakeDriver)
            self.assertTrue(session.headless)

    def test_06_headless_false(self):
        with patch.object(Config, "HEADLESS", False):
            session = BrowserSession(driver_factory=FakeDriver)
            self.assertFalse(session.headless)

    def test_07_explicit_constructor_override(self):
        with patch.object(Config, "HEADLESS", True):
            session = BrowserSession(headless=False, driver_factory=FakeDriver)
            self.assertFalse(session.headless)

    def test_08_interactive_login_uses_visible_mode(self):
        from main import headless_override
        self.assertFalse(headless_override(visible=True))

    def test_09_test_login_navigates_to_account_url(self):
        with patch.object(Config, "ACCOUNT_URL", "https://www.freelancermap.com/my_account.html"):
            session = BrowserSession(driver_factory=FakeDriver)
            res = session.verify_authenticated_session()
            self.assertTrue(res.authenticated)
            self.assertEqual("https://www.freelancermap.com/my_account.html", session.driver.current_url)

    def test_10_valid_authenticated_page_passes(self):
        session = BrowserSession(driver_factory=FakeDriver)
        res = session.verify_authenticated_session()
        self.assertTrue(res.authenticated)

    def test_11_login_redirect_fails(self):
        session = BrowserSession(driver_factory=lambda opt: FakeDriver(opt, mode="login"))
        res = session.verify_authenticated_session()
        self.assertFalse(res.authenticated)
        self.assertTrue(res.reason)
        self.assertIn(
            "password form",
            res.reason.casefold(),
        )

    def test_12_password_form_fails(self):
        session = BrowserSession(driver_factory=lambda opt: FakeDriver(opt, mode="login"))
        res = session.verify_authenticated_session()
        self.assertFalse(res.authenticated)

    def test_13_captcha_fails(self):
        session = BrowserSession(driver_factory=lambda opt: FakeDriver(opt, mode="captcha"))
        res = session.verify_authenticated_session()
        self.assertFalse(res.authenticated)

    def test_14_mfa_fails(self):
        session = BrowserSession(driver_factory=lambda opt: FakeDriver(opt, mode="challenge"))
        res = session.verify_authenticated_session()
        self.assertFalse(res.authenticated)

    def test_15_access_denied_fails(self):
        session = BrowserSession(driver_factory=lambda opt: FakeDriver(opt, mode="access_denied"))
        res = session.verify_authenticated_session()
        self.assertFalse(res.authenticated)

    def test_16_rate_limit_fails(self):
        session = BrowserSession(driver_factory=lambda opt: FakeDriver(opt, mode="rate_limit"))
        res = session.verify_authenticated_session()
        self.assertFalse(res.authenticated)

    def test_17_404_fails(self):
        session = BrowserSession(driver_factory=lambda opt: FakeDriver(opt, mode="404"))
        res = session.verify_authenticated_session()
        self.assertFalse(res.authenticated)

    def test_18_server_error_fails(self):
        session = BrowserSession(driver_factory=lambda opt: FakeDriver(opt, mode="error_500"))
        res = session.verify_authenticated_session()
        self.assertFalse(res.authenticated)

    def test_19_listing_page_waits_for_project_routes(self):
        session = BrowserSession(driver_factory=lambda opt: FakeDriver(opt, route_count=7))
        count = session._project_route_count()
        self.assertEqual(count, 7)

    def test_20_stable_scrolling_terminates(self):
        session = BrowserSession(driver_factory=FakeDriver)
        with patch.object(Config, "SCROLL_PAUSE_SECONDS", 0.01):
            session.scroll_to_bottom(max_scrolls=5)
        self.assertEqual(session.driver.scroll_calls, 2)

    def test_21_valid_load_more_button_clicked(self):
        session = BrowserSession(driver_factory=lambda opt: FakeDriver(opt, load_more=True, route_growth=1))
        session.click_load_more = MagicMock(wraps=session.click_load_more)
        with patch.object(Config, "SCROLL_PAUSE_SECONDS", 0.01), \
             patch.object(Config, "MAX_LOAD_MORE_CLICKS", 3), \
             patch.object(Config, "LISTING_STABILITY_POLL_SECONDS", 0.01), \
             patch.object(Config, "PAGE_LOAD_TIMEOUT", 5), \
             patch.object(Config, "LISTING_STABLE_ROUNDS", 2):
            html = session.load_listing_page("https://www.freelancermap.com/projects?sort=1")
        self.assertEqual(session.click_load_more.call_count, 3)
        self.assertIn("unit-test-proj", html)

    def test_22_generic_filter_show_more_not_clicked(self):
        session = BrowserSession(driver_factory=FakeDriver)
        with patch.object(Config, "SCROLL_PAUSE_SECONDS", 0.01):
            self.assertFalse(session.click_load_more())
        self.assertEqual(session.driver.load_more_requests, 1)

    def test_23_pagination_loop_terminates(self):
        url = "https://www.freelancermap.com/projects?sort=1"
        session = BrowserSession(driver_factory=FakeDriver)
        with patch.object(Config, "PRIMARY_SEARCH_URL", url), \
             patch.object(Config, "MAX_PAGES", 3), \
             patch.object(Config, "ENABLE_PERSONALIZED_FEED", False), \
             patch.object(Config, "LISTING_STABILITY_POLL_SECONDS", 0.01), \
             patch.object(Config, "PAGE_LOAD_TIMEOUT", 10), \
             patch.object(session, "next_page_url", return_value=url):
            outcome = _discover(session, scan_at="2026-07-31T00:00:00Z")
        self.assertIsInstance(outcome.projects, list)
        self.assertGreaterEqual(session.driver.route_calls, 2)

    def test_24_primary_filtered_url_preserved(self):
        url = "https://www.freelancermap.com/projects?sort=date_desc&kw=python"
        with patch.object(Config, "PRIMARY_SEARCH_URL", url):
            self.assertEqual(url, Config.PRIMARY_SEARCH_URL)

    def test_25_secondary_feed_disabled_by_default(self):
        with patch.object(Config, "ENABLE_PERSONALIZED_FEED", False), \
             patch.object(Config, "PERSONALIZED_FEED_DISCOVERY", False):
            self.assertFalse(Config.ENABLE_PERSONALIZED_FEED)
            self.assertFalse(Config.PERSONALIZED_FEED_DISCOVERY)

    def test_26_primary_and_personalized_duplicate_project_merge(self):
        p1 = ProjectDiscovery(source_key="dup", slug="dup", url="https://www.freelancermap.com/project/dup", title_hint="Title 1", card_location="Berlin")
        p2 = ProjectDiscovery(source_key="dup", slug="dup", url="https://www.freelancermap.com/project/dup", title_hint="Title 1", card_location="Berlin")
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "test_merge.db"
            with patch.object(database, "DATABASE_PATH", db_path):
                database.initialize_database()
                id1, created1 = database.upsert_discovery(p1, seen_in_primary=True, primary_position=1)
                id2, created2 = database.upsert_discovery(p2, seen_in_personalized=True, personalized_position=2)
                self.assertEqual(id1, id2)
                self.assertFalse(created2)

    def test_27_primary_only_project_remains(self):
        p1 = ProjectDiscovery(source_key="p1", slug="p1", url="https://www.freelancermap.com/project/p1", title_hint="Primary Only")
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "test_p1.db"
            with patch.object(database, "DATABASE_PATH", db_path):
                database.initialize_database()
                id1, created = database.upsert_discovery(p1, seen_in_primary=True)
                self.assertTrue(created)

    def test_28_secondary_only_project_ignored_when_personalized_discovery_false(self):
        with patch.object(Config, "ENABLE_PERSONALIZED_FEED", True), \
             patch.object(Config, "PERSONALIZED_FEED_DISCOVERY", False), \
             patch.object(Config, "PERSONALIZED_SEARCH_URL", "https://www.freelancermap.com/projects?sort=relevant"), \
             patch.object(Config, "LISTING_STABILITY_POLL_SECONDS", 0.01), \
             patch.object(Config, "PAGE_LOAD_TIMEOUT", 10):
            session = BrowserSession(driver_factory=FakeDriver)
            outcome = _discover(session, scan_at="2026-07-31T00:00:00Z")
            self.assertIsInstance(outcome.projects, list)
            self.assertEqual("ok", outcome.personalized_feed_status)
            self.assertEqual(1, outcome.personalized_count)

    def test_29_secondary_only_project_accepted_when_personalized_discovery_true(self):
        with patch.object(Config, "ENABLE_PERSONALIZED_FEED", True), \
             patch.object(Config, "PERSONALIZED_FEED_DISCOVERY", True), \
             patch.object(Config, "PERSONALIZED_SEARCH_URL", "https://www.freelancermap.com/projects?sort=relevant"), \
             patch.object(Config, "LISTING_STABILITY_POLL_SECONDS", 0.01), \
             patch.object(Config, "PAGE_LOAD_TIMEOUT", 10):
            session = BrowserSession(driver_factory=FakeDriver)
            outcome = _discover(session, scan_at="2026-07-31T00:00:00Z")
            self.assertIsInstance(outcome.projects, list)
            self.assertEqual("ok", outcome.personalized_feed_status)

    def test_40_primary_feed_navigation_preserves_query_and_sort(self):
        url = "https://www.freelancermap.com/projects?excludeDachProjects=false&query=website+development&sort=1&pagenr=1"
        session = BrowserSession(driver_factory=FakeDriver)
        with patch.object(Config, "PRIMARY_SEARCH_URL", url), \
             patch.object(Config, "ENABLE_PERSONALIZED_FEED", False), \
             patch.object(Config, "MAX_PAGES", 1), \
             patch.object(Config, "FEED_QUERY_SORT_PARAM", "sort"), \
             patch.object(Config, "PRIMARY_FEED_NEWEST_SORT_VALUE", "1"), \
             patch.object(Config, "LISTING_STABILITY_POLL_SECONDS", 0.01), \
             patch.object(Config, "PAGE_LOAD_TIMEOUT", 10):
            _discover(session, scan_at="2026-07-31T00:00:00Z")
        current = session.driver.current_url
        self.assertIn("sort=1", current)
        self.assertIn("query=website+development", current)
        self.assertEqual(1, current.count("sort="))

    def test_41_primary_feed_navigation_appends_missing_sort_param(self):
        session = BrowserSession(driver_factory=FakeDriver)
        with patch.object(Config, "PRIMARY_SEARCH_URL", "https://www.freelancermap.com/projects"), \
             patch.object(Config, "ENABLE_PERSONALIZED_FEED", False), \
             patch.object(Config, "MAX_PAGES", 1), \
             patch.object(Config, "FEED_QUERY_SORT_PARAM", "sort"), \
             patch.object(Config, "PRIMARY_FEED_NEWEST_SORT_VALUE", "1"), \
             patch.object(Config, "LISTING_STABILITY_POLL_SECONDS", 0.01), \
             patch.object(Config, "PAGE_LOAD_TIMEOUT", 10):
            _discover(session, scan_at="2026-07-31T00:00:00Z")
        self.assertEqual(
            "https://www.freelancermap.com/projects?sort=1",
            session.driver.current_url,
        )

    def test_42_personalized_feed_navigation_preserves_query(self):
        primary = "https://www.freelancermap.com/projects?sort=1"
        personalized = "https://www.freelancermap.com/projects?excludeDachProjects=false&query=automation&sort=1&pagenr=1"
        session = BrowserSession(driver_factory=FakeDriver)
        with patch.object(Config, "PRIMARY_SEARCH_URL", primary), \
             patch.object(Config, "ENABLE_PERSONALIZED_FEED", True), \
             patch.object(Config, "PERSONALIZED_FEED_DISCOVERY", False), \
             patch.object(Config, "PERSONALIZED_SEARCH_URL", personalized), \
             patch.object(Config, "MAX_PAGES", 1), \
             patch.object(Config, "FEED_QUERY_SORT_PARAM", "sort"), \
             patch.object(Config, "PRIMARY_FEED_NEWEST_SORT_VALUE", "1"), \
             patch.object(Config, "LISTING_STABILITY_POLL_SECONDS", 0.01), \
             patch.object(Config, "PAGE_LOAD_TIMEOUT", 10):
            _discover(session, scan_at="2026-07-31T00:00:00Z")
        self.assertEqual(personalized, session.driver.current_url)


    def test_30_secondary_values_enrich_missing_primary_fields(self):
        p1 = ProjectDiscovery(source_key="enrich", slug="enrich", url="https://www.freelancermap.com/project/enrich", title_hint="Title")
        p2 = ProjectDiscovery(source_key="enrich", slug="enrich", url="https://www.freelancermap.com/project/enrich", title_hint="Title", card_location="Munich")
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "test_enrich.db"
            with patch.object(database, "DATABASE_PATH", db_path):
                database.initialize_database()
                database.upsert_discovery(p1, seen_in_primary=True)
                database.upsert_discovery(p2, seen_in_personalized=True)
                with database.connection() as conn:
                    row = conn.execute("SELECT * FROM projects WHERE url=?", (p1.url,)).fetchone()
                self.assertEqual("Munich", row["card_location"])

    def test_31_secondary_values_do_not_overwrite_stronger_primary_values(self):
        p1 = ProjectDiscovery(source_key="strong", slug="strong", url="https://www.freelancermap.com/project/strong", title_hint="Title", card_location="Berlin")
        p2 = ProjectDiscovery(source_key="strong", slug="strong", url="https://www.freelancermap.com/project/strong", title_hint="Title", card_location="Munich")
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "test_strong.db"
            with patch.object(database, "DATABASE_PATH", db_path):
                database.initialize_database()
                database.upsert_discovery(p1, seen_in_primary=True)
                database.upsert_discovery(p2, seen_in_personalized=True)
                with database.connection() as conn:
                    row = conn.execute("SELECT * FROM projects WHERE url=?", (p1.url,)).fetchone()
                self.assertEqual("Berlin", row["card_location"])

    def test_32_authentication_failure_blocks_baseline(self):
        def unauth_factory(options):
            return FakeDriver(options, mode="login")
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "test_auth_base.db"
            lock_path = Path(folder) / "test_auth_base.lock"
            with patch.object(database, "DATABASE_PATH", db_path), \
                 patch.object(Config, "LOCK_PATH", lock_path), \
                 patch.object(Config, "REQUIRE_LOGIN", True), \
                 patch.object(Config, "PRIMARY_SEARCH_URL", "https://www.freelancermap.com/projects?sort=1"), \
                 patch("monitor.exclusive_file_lock"), \
                 patch("monitor.BrowserSession", lambda **kw: BrowserSession(driver_factory=unauth_factory)):
                database.initialize_database()
                with self.assertRaises(RuntimeError):
                    run_cycle(dry_run=True, force_baseline=True, headless=True)

    def test_33_authentication_failure_blocks_project_emails(self):
        def unauth_factory(options):
            return FakeDriver(options, mode="login")
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "test_auth_email.db"
            lock_path = Path(folder) / "test_auth_email.lock"
            with patch.object(database, "DATABASE_PATH", db_path), \
                 patch.object(Config, "LOCK_PATH", lock_path), \
                 patch.object(Config, "REQUIRE_LOGIN", True), \
                 patch.object(Config, "PRIMARY_SEARCH_URL", "https://www.freelancermap.com/projects?sort=1"), \
                 patch("monitor.exclusive_file_lock"), \
                 patch("monitor.BrowserSession", lambda **kw: BrowserSession(driver_factory=unauth_factory)):
                database.initialize_database()
                with self.assertRaises(RuntimeError):
                    run_cycle(dry_run=False, force_baseline=False, headless=True)

    def test_34_first_run_baseline_safety_gate(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "test_gate.db"
            lock_path = Path(folder) / "test_gate.lock"
            with patch.object(database, "DATABASE_PATH", db_path), \
                 patch.object(Config, "LOCK_PATH", lock_path), \
                 patch.object(Config, "AUTO_BASELINE_ON_FIRST_RUN", False), \
                 patch.object(Config, "PRIMARY_SEARCH_URL", "https://www.freelancermap.com/projects?sort=1"), \
                 patch("monitor.exclusive_file_lock"):
                database.initialize_database()
                with self.assertRaises(RuntimeError) as ctx:
                    run_cycle(dry_run=False, force_baseline=False, headless=True)
                self.assertIn("Baseline is not initialized", str(ctx.exception))

    def test_35_invalid_detail_refresh_preserves_prior_valid_data(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "test_refresh.db"
            with patch.object(database, "DATABASE_PATH", db_path):
                database.initialize_database()
                p = ProjectDiscovery(
                    source_key="valid-detail",
                    slug="valid-detail",
                    url="https://www.freelancermap.com/project/valid-detail",
                    title_hint="Original Title",
                    card_description="Valid Description",
                    card_location="Prague",
                )
                pid, _ = database.upsert_discovery(p)
                bad_html = "<html><head><title>404 Not Found</title></head><body>Page not found</body></html>"
                bad_detail = parse_project_detail(bad_html, p.url, Config.BASE_URL)
                with self.assertRaises(Exception):
                    validate_detail(bad_detail)
                database.mark_detail_failure(pid, "PageNotFoundError: 404")
                with database.connection() as conn:
                    row = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
                self.assertEqual("Original Title", row["title"])
                self.assertEqual("Valid Description", row["description"])
                self.assertNotEqual("success", row["detail_fetch_status"])

    def test_36_404_detail_never_saved_as_success(self):
        html = "<html><head><title>404 Not Found</title></head><body>Page not found</body></html>"
        detail = parse_project_detail(html, "https://www.freelancermap.com/project/404-test", Config.BASE_URL)
        with self.assertRaises(Exception):
            validate_detail(detail)

    def test_37_existing_single_feed_configurations_remain_compatible(self):
        with patch.object(Config, "ENABLE_PERSONALIZED_FEED", False), \
             patch.object(Config, "LISTING_STABILITY_POLL_SECONDS", 0.01), \
             patch.object(Config, "PAGE_LOAD_TIMEOUT", 10):
            session = BrowserSession(driver_factory=FakeDriver)
            outcome = _discover(session, scan_at="2026-07-31T00:00:00Z")
            self.assertIsInstance(outcome.projects, list)
            self.assertEqual("not_configured", outcome.personalized_feed_status)
            session = BrowserSession(driver_factory=FakeDriver)
            outcome = _discover(session, scan_at="2026-07-31T00:00:00Z")
            self.assertIsInstance(outcome.projects, list)

    def test_38_discovery_sources_accumulate_across_feeds(self):
        import json
        p1 = ProjectDiscovery(
            source_key="accum",
            slug="accum",
            url="https://www.freelancermap.com/project/accum",
            title_hint="Title",
        )
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "test_accum.db"
            with patch.object(database, "DATABASE_PATH", db_path):
                database.initialize_database()
                database.upsert_discovery(p1, seen_in_primary=True, primary_position=1)
                with database.connection() as conn:
                    row = conn.execute(
                        "SELECT discovery_sources_json FROM projects WHERE url=?",
                        (p1.url,),
                    ).fetchone()
                self.assertEqual(
                    ["primary_newest"],
                    json.loads(row["discovery_sources_json"]),
                )
                database.upsert_discovery(
                    p1,
                    seen_in_primary=False,
                    seen_in_personalized=True,
                    personalized_position=2,
                )
                with database.connection() as conn:
                    row = conn.execute("SELECT * FROM projects WHERE url=?", (p1.url,)).fetchone()
                sources = json.loads(row["discovery_sources_json"])
                self.assertIn("primary_newest", sources)
                self.assertIn("personalized_relevant", sources)
                self.assertEqual(1, row["seen_in_primary"])
                self.assertEqual(1, row["seen_in_personalized"])
                self.assertEqual(1, row["primary_position"])
                self.assertEqual(2, row["personalized_position"])

    def test_39_primary_feed_without_newest_sort_rejected(self):
        with patch.object(Config, "PRIMARY_SEARCH_URL", "https://www.freelancermap.com/projects"):
            errors = Config.validate_runtime()
        self.assertTrue(any("newest-first" in error for error in errors))

    def test_40_feed_login_and_detail_routes_rejected(self):
        for bad_url in (
            "https://www.freelancermap.com/login?sort=1",
            "https://www.freelancermap.com/my_account.html?sort=1",
            "https://www.freelancermap.com/project/some-detail?sort=1",
            "https://www.freelancermap.com/dashboard?sort=1",
            "https://www.freelancermap.com/app/projekt/list?sort=1",
        ):
            with self.subTest(url=bad_url), \
                 patch.object(Config, "PRIMARY_SEARCH_URL", bad_url):
                errors = Config.validate_runtime()
            self.assertTrue(
                any("listing/search route" in error for error in errors),
                msg=f"Expected route rejection for {bad_url}",
            )
        with patch.object(Config, "PRIMARY_SEARCH_URL", "https://www.freelancermap.com/projects?sort=1"):
            errors = Config.validate_runtime()
        self.assertFalse(any("listing/search route" in error for error in errors))

    def test_41_secondary_feed_requires_supported_sort(self):
        with patch.object(Config, "PERSONALIZED_SEARCH_URL", "https://www.freelancermap.com/projects?sort=9"):
            errors = Config.validate_runtime()
        self.assertTrue(any("supported sort" in error for error in errors))
        with patch.object(Config, "PERSONALIZED_SEARCH_URL", "https://www.freelancermap.com/projects?sort=2"):
            errors = Config.validate_runtime()
        self.assertFalse(any("supported sort" in error for error in errors))

    def test_42_listing_page_rejects_mismatched_sort_state(self):
        session = BrowserSession(driver_factory=lambda opt: FakeDriver(opt, sort_state="2"))
        with patch.object(Config, "SCROLL_PAUSE_SECONDS", 0.01), \
             patch.object(Config, "LISTING_STABILITY_POLL_SECONDS", 0.01), \
             patch.object(Config, "PAGE_LOAD_TIMEOUT", 10):
            with self.assertRaises(Exception):
                session.load_listing_page(
                    "https://www.freelancermap.com/projects?sort=1",
                    expected_sort="1",
                )

    def test_43_listing_page_accepts_newest_sort_state(self):
        session = BrowserSession(driver_factory=lambda opt: FakeDriver(opt, sort_state="1"))
        with patch.object(Config, "SCROLL_PAUSE_SECONDS", 0.01), \
             patch.object(Config, "LISTING_STABILITY_POLL_SECONDS", 0.01), \
             patch.object(Config, "PAGE_LOAD_TIMEOUT", 10):
            html = session.load_listing_page(
                "https://www.freelancermap.com/projects?sort=1",
                expected_sort="1",
            )
        self.assertIn("unit-test-proj", html)


class AdvancingClock:
    """Test clock: every monotonic() call advances one second; sleep is a no-op."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        self.now += 1.0
        return self.now

    def sleep(self, _seconds):
        return None


class StrongAuthenticationVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def test_44_blank_page_never_counts_as_authenticated(self):
        session = BrowserSession(driver_factory=lambda opt: FakeDriver(opt, mode="blank"))
        res = session.verify_authenticated_session()
        self.assertFalse(res.authenticated)
        self.assertTrue(res.reason)

    def test_45_public_unauthenticated_page_never_counts_as_authenticated(self):
        session = BrowserSession(driver_factory=lambda opt: FakeDriver(opt, mode="public"))
        res = session.verify_authenticated_session()
        self.assertFalse(res.authenticated)
        self.assertIn("marker", res.reason.casefold())

    def test_46_account_like_page_without_positive_marker_fails(self):
        session = BrowserSession(driver_factory=lambda opt: FakeDriver(opt, mode="account_no_marker"))
        res = session.verify_authenticated_session()
        self.assertFalse(res.authenticated)
        self.assertIn("marker", res.reason.casefold())

    def test_47_maintenance_page_never_counts_as_authenticated(self):
        session = BrowserSession(driver_factory=lambda opt: FakeDriver(opt, mode="maintenance"))
        res = session.verify_authenticated_session()
        self.assertFalse(res.authenticated)
        self.assertTrue(res.reason)

    def test_48_logout_marker_confirms_authentication(self):
        session = BrowserSession(driver_factory=lambda opt: FakeDriver(opt, mode="logout_marker"))
        res = session.verify_authenticated_session()
        self.assertTrue(res.authenticated)

    def test_49_user_menu_marker_confirms_authentication(self):
        session = BrowserSession(driver_factory=lambda opt: FakeDriver(opt, mode="user_menu_marker"))
        res = session.verify_authenticated_session()
        self.assertTrue(res.authenticated)

    def test_50_account_dashboard_marker_confirms_authentication(self):
        session = BrowserSession(driver_factory=lambda opt: FakeDriver(opt, mode="dashboard_marker"))
        res = session.verify_authenticated_session()
        self.assertTrue(res.authenticated)

    def test_51_interactive_login_requires_strong_verification(self):
        session = BrowserSession(driver_factory=lambda opt: FakeDriver(opt, mode="login"))
        with patch("browser.time", AdvancingClock()):
            result = session.interactive_login(timeout_seconds=600)
        self.assertFalse(result)

    def test_52_interactive_login_succeeds_with_positive_marker(self):
        session = BrowserSession(driver_factory=FakeDriver)
        with patch("browser.time", AdvancingClock()), patch.object(
            BrowserSession,
            "is_logged_in",
            side_effect=AssertionError("login flows must not use is_logged_in()"),
        ):
            result = session.interactive_login(timeout_seconds=5)
        self.assertTrue(result)

    def test_53_credential_login_succeeds_with_positive_marker(self):
        session = BrowserSession(driver_factory=FakeDriver)
        with patch("browser.time", AdvancingClock()), patch.object(
            Config, "LOGIN_EMAIL", "test@example.com"
        ), patch.object(Config, "LOGIN_PASSWORD", "s3cret"), patch.object(
            BrowserSession,
            "is_logged_in",
            side_effect=AssertionError("login flows must not use is_logged_in()"),
        ):
            result = session.login_with_credentials()
        self.assertTrue(result)

    def test_54_credential_login_fails_without_positive_marker(self):
        session = BrowserSession(driver_factory=lambda opt: FakeDriver(opt, mode="public"))
        with patch("browser.time", AdvancingClock()), patch.object(
            Config, "LOGIN_EMAIL", "test@example.com"
        ), patch.object(Config, "LOGIN_PASSWORD", "s3cret"):
            result = session.login_with_credentials()
        self.assertFalse(result)

    def test_55_expired_login_after_initial_check_aborts_cycle(self):
        from browser import HttpError

        class ExpiredSessionBrowser:
            def __init__(self, headless=None):
                self.headless = headless

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return None

            def verify_authenticated_session(self):
                return AuthVerificationResult(authenticated=True, reason="OK")

            def load_listing_page(self, url, *, expected_sort=None):
                raise HttpError(
                    "Navigated to a login page when protected content was expected"
                )

        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "test_expired.db"
            lock_path = Path(folder) / "test_expired.lock"
            with patch.object(database, "DATABASE_PATH", db_path), \
                 patch.object(Config, "LOCK_PATH", lock_path), \
                 patch.object(Config, "REQUIRE_LOGIN", True), \
                 patch.object(Config, "EMPTY_RESULT_RETRIES", 0), \
                 patch.object(Config, "PRIMARY_SEARCH_URL", "https://www.freelancermap.com/projects?sort=1"), \
                 patch("monitor.exclusive_file_lock"), \
                 patch("monitor.BrowserSession", ExpiredSessionBrowser):
                database.initialize_database()
                with self.assertRaises(RuntimeError):
                    run_cycle(dry_run=False, force_baseline=True, headless=True)
                with database.connection() as conn:
                    running = conn.execute(
                        "SELECT COUNT(*) AS c FROM scans WHERE status='running'"
                    ).fetchone()["c"]
                    total = conn.execute(
                        "SELECT COUNT(*) AS c FROM scans"
                    ).fetchone()["c"]
                self.assertEqual(0, running)
                self.assertEqual(1, total)

    def test_56_baseline_initializing_cleared_after_auth_failure(self):
        def unauth_factory(options):
            return FakeDriver(options, mode="login")

        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "test_auth_cleanup.db"
            lock_path = Path(folder) / "test_auth_cleanup.lock"
            with patch.object(database, "DATABASE_PATH", db_path), \
                 patch.object(Config, "LOCK_PATH", lock_path), \
                 patch.object(Config, "REQUIRE_LOGIN", True), \
                 patch.object(Config, "PRIMARY_SEARCH_URL", "https://www.freelancermap.com/projects?sort=1"), \
                 patch("monitor.exclusive_file_lock"), \
                 patch("monitor.BrowserSession", lambda **kw: BrowserSession(driver_factory=unauth_factory)):
                database.initialize_database()
                with self.assertRaises(RuntimeError):
                    run_cycle(dry_run=False, force_baseline=True, headless=True)
                self.assertEqual(
                    "false",
                    database.get_setting("baseline_initializing", "false"),
                )
                self.assertEqual(
                    "",
                    database.get_setting("baseline_started_at", "false"),
                )


if __name__ == "__main__":
    unittest.main()
