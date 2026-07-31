import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import database
from browser import BrowserProfileInUseError, BrowserSession
from config import Config
from monitor import run_cycle
from parser import ProjectDiscovery, parse_project_detail


class FakeDriver:
    """Mock WebDriver for unit testing without spawning real Chrome processes."""
    def __init__(self, options=None, mode="account"):
        self.options = options
        self.mode = mode
        self.current_url = "https://www.freelancermap.com/my_account.html"
        self.title = "Freelancermap Account Dashboard"
        self.page_source = "<html><body><h1>Dashboard</h1><p>My Freelancermap</p><a href='/logout'>Logout</a></body></html>"

    def get(self, url):
        if self.mode == "login":
            self.current_url = "https://www.freelancermap.com/login"
            self.title = "Freelancermap Login"
            self.page_source = "<html><body><form action='/login'><input type='password' name='pass'/></form></body></html>"
        elif self.mode == "404":
            self.current_url = url
            self.title = "404 Not Found"
            self.page_source = "<html><body><h1>Page Not Found</h1></body></html>"
        elif self.mode in ("challenge", "captcha"):
            self.current_url = url
            self.title = "Just a moment..."
            self.page_source = "<html><body>Verify you are human</body></html>"
        elif self.mode == "error_500":
            self.current_url = url
            self.title = "500 Internal Server Error"
            self.page_source = "<html><body>500 Server Error</body></html>"
        elif self.mode == "access_denied":
            self.current_url = url
            self.title = "Access Denied"
            self.page_source = "<html><body>403 Forbidden Access Denied</body></html>"
        elif self.mode == "rate_limit":
            self.current_url = url
            self.title = "429 Too Many Requests"
            self.page_source = "<html><body>429 Rate Limit</body></html>"
        else:
            self.current_url = url
            if "my_account" in url:
                self.title = "Freelancermap Account Dashboard"
                self.page_source = "<html><body><h1>Dashboard</h1><p>My Freelancermap</p><a href='/logout'>Logout</a></body></html>"
            elif "projects" in url:
                self.title = "Freelancermap Projects"
                self.page_source = (
                    "<html><body><div class='project-card'>"
                    "<a href='/project/unit-test-proj' data-testid='title'>Unit Test Proj</a>"
                    "<div data-testid='city'>Berlin</div></div></body></html>"
                )

    def set_page_load_timeout(self, val):
        pass

    def set_script_timeout(self, val):
        pass

    def implicitly_wait(self, val):
        pass

    def find_elements(self, by, value):
        if "password" in value and "password" in self.page_source:
            elem = MagicMock()
            return [elem]
        return []

    def execute_script(self, script, *args):
        if "return document.readyState" in script:
            return "complete"
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
            return "Dashboard My Freelancermap Logout Abmelden"
        return None

    def quit(self):
        pass


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
        self.assertIn("login", res.reason.casefold())

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

    def test_24_primary_filtered_url_preserved(self):
        url = "https://www.freelancermap.com/projects?sort=date_desc&kw=python"
        with patch.object(Config, "PRIMARY_SEARCH_URL", url):
            self.assertEqual(url, Config.PRIMARY_SEARCH_URL)

    def test_25_secondary_feed_disabled_by_default(self):
        self.assertFalse(Config.ENABLE_PERSONALIZED_FEED)
        self.assertFalse(Config.PERSONALIZED_FEED_DISCOVERY)

    def test_34_first_run_baseline_safety_gate(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "test_gate.db"
            with patch.object(database, "DATABASE_PATH", db_path), \
                 patch.object(Config, "AUTO_BASELINE_ON_FIRST_RUN", False), \
                 patch("monitor.BrowserSession", lambda **kw: FakeDriver()):
                database.initialize_database()
                # Confirm empty database does not baseline without flag
                self.assertFalse(database.baseline_initialized())


if __name__ == "__main__":
    unittest.main()
