from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse, urljoin, urlunsplit

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from config import Config


LOGGER = logging.getLogger(__name__)


class BrowserNavigationError(Exception):
    """Raised when a navigation target fails origin or safety checks."""


@dataclass
class PageState:
    """Snapshot of the current browser page classification."""

    ready: bool = False
    login_required: bool = False
    challenge_detected: bool = False
    error_detected: bool = False
    empty: bool = False


LOGIN_PATH_RE = re.compile(r"/(?:login|sign-in)(?:/|$)", re.I)
CHALLENGE_TITLE_RE = re.compile(
    r"(?:just a moment|verify you are human|attention required|checking your browser|"
    r"enable javascript|ray id|cloudflare|captcha|recaptcha challenge)",
    re.I,
)
CHALLENGE_BODY_RE = re.compile(
    r"(?:verify you are human|checking your browser|enable javascript|"
    r"please wait|security check|access denied|ray id)",
    re.I,
)
ERROR_TITLE_RE = re.compile(
    r"(?:service unavailable|internal server error|bad gateway|"
    r"temporarily unavailable|error|503|502|403|forbidden)",
    re.I,
)
ERROR_BODY_RE = re.compile(
    r"(?:service unavailable|internal server error|bad gateway|"
    r"temporarily unavailable|something went wrong|try again later)",
    re.I,
)
RECAPTCHA_SCRIPT_ONLY_RE = re.compile(
    r"<script[^>]+src=['\"]https?://[^'\"]*recaptcha[^\"]*['\"]",
    re.I,
)


def _is_login_url(url: str) -> bool:
    """Return True if the URL path indicates a login or sign-in page."""
    try:
        parsed = urlparse(url)
        return bool(LOGIN_PATH_RE.search(parsed.path))
    except Exception:
        return False


class BrowserSession:
    """Selenium Chrome session with origin-safe navigation guards."""

    def __init__(self, headless: bool | None = True) -> None:
        self.headless = True if headless is None else bool(headless)
        self.driver: WebDriver | None = None
        self._ensure_driver()

    def _ensure_driver(self) -> None:
        if self.driver is not None:
            return
        options = Options()
        if self.headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument(
            "--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        self.driver = webdriver.Chrome(options=options)
        self.driver.implicitly_wait(3)

    def close(self) -> None:
        """Quit the underlying driver and release resources."""
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None

    def __enter__(self) -> "BrowserSession":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _same_origin(url: str, base: str) -> bool:
        """Check whether *url* shares an origin with *base*."""
        try:
            url_parts = urlparse(url)
            base_parts = urlparse(base)
            url_host = (url_parts.hostname or "").casefold()
            base_host = (base_parts.hostname or "").casefold()
            if url_host != base_host:
                return False
            url_port = url_parts.port
            base_port = base_parts.port
            default_ports = {"http": 80, "https": 443}
            effective_url_port = url_port or default_ports.get(url_parts.scheme.casefold())
            effective_base_port = base_port or default_ports.get(base_parts.scheme.casefold())
            return effective_url_port == effective_base_port
        except Exception:
            return False

    def _validated_navigation_url(self, url: str) -> str:
        """Validate and normalize a navigation target.

        Raises BrowserNavigationError for insecure schemes, cross-origin
        targets, embedded credentials, or control characters.

        Unlike ``canonicalize_url`` used by the parser, this method preserves
        query strings because navigation targets often rely on pagination
        parameters.
        """
        from utils import _clean_url_input, _split_http_url, _validated_hostname_and_port, _normalize_hostname, _normalize_percent_escape_case

        value = url.strip()
        if not value:
            raise BrowserNavigationError("URL must not be empty")

        if "\n" in value or "\r" in value or "\x00" in value:
            raise BrowserNavigationError("URL contains control characters")

        cleaned = _clean_url_input(value, field_name="url")
        base = _clean_url_input(Config.BASE_URL, field_name="base_url")
        join_base = base if base.endswith("/") else f"{base}/"
        resolved = urljoin(join_base, cleaned)

        parts = _split_http_url(resolved, field_name="url")
        if parts.username is not None or parts.password is not None:
            raise BrowserNavigationError("URL must not contain embedded credentials")

        hostname, port = _validated_hostname_and_port(parts, field_name="url")
        normalized_host = _normalize_hostname(hostname)
        if ":" in normalized_host and not normalized_host.startswith("["):
            normalized_host = f"[{normalized_host}]"

        scheme = parts.scheme.casefold()

        if scheme == "http" and not Config.ALLOW_INSECURE_HTTP:
            raise BrowserNavigationError("Insecure HTTP navigation is not allowed")

        base_parts = _split_http_url(base, field_name="base_url")
        base_host = _normalize_hostname(base_parts.hostname or "")
        base_port = base_parts.port
        default_ports = {"http": 80, "https": 443}
        effective_port = port or default_ports.get(scheme)
        effective_base_port = base_port or default_ports.get(base_parts.scheme.casefold())

        if normalized_host != base_host or effective_port != effective_base_port:
            if not Config.ALLOW_CROSS_ORIGIN_URLS:
                raise BrowserNavigationError("Cross-origin navigation is not allowed")

        path = parts.path or "/"
        path = _normalize_percent_escape_case(path)
        if path != "/":
            path = path.rstrip("/") or "/"

        netloc = normalized_host if port in (None, default_ports.get(scheme)) else f"{normalized_host}:{port}"
        return urlunsplit((scheme, netloc, path, parts.query, ""))

    def _project_route_count(self) -> int:
        """Count unique /project/ routes in the current page DOM."""
        try:
            return self.driver.execute_script(
                "var routes = new Set();"
                "document.querySelectorAll('a[href], [data-href], [data-url]').forEach(function(el) {"
                "  var attr = el.getAttribute('href') || el.getAttribute('data-href') || el.getAttribute('data-url') || '';"
                "  var match = attr.match(/\\/project\\/[^/?#]+/);"
                "  if (match) routes.add(match[0]);"
                "});"
                "return routes.size;"
            ) or 0
        except Exception:
            return 0

    def _page_state(self) -> PageState:
        """Classify the current page into a PageState."""
        try:
            title = (self.driver.title or "").strip()
            body_text = self._body_text()
            page_source = self.driver.page_source or ""

            ready = self._is_page_ready(body_text)

            # Challenge detection: visible human verification elements
            challenge = bool(
                CHALLENGE_TITLE_RE.search(title) or CHALLENGE_BODY_RE.search(body_text)
            )

            # A recaptcha script tag alone is NOT a challenge; only the
            # interactive challenge widget counts.
            if challenge and not CHALLENGE_TITLE_RE.search(title):
                if RECAPTCHA_SCRIPT_ONLY_RE.search(page_source) and not CHALLENGE_BODY_RE.search(body_text):
                    challenge = False

            error = bool(ERROR_TITLE_RE.search(title) or ERROR_BODY_RE.search(body_text))

            login_required = _is_login_url(self.driver.current_url)

            empty = not bool(body_text.strip())

            return PageState(
                ready=ready,
                login_required=login_required,
                challenge_detected=challenge,
                error_detected=error,
                empty=empty,
            )
        except Exception:
            return PageState(error_detected=True)

    def _is_page_ready(self, body_text: str) -> bool:
        """Check if the page has loaded past the initial state."""
        try:
            ready_state = self.driver.execute_script("return document.readyState")
            if ready_state == "complete":
                return True
        except Exception:
            pass
        return False

    def _body_text(self) -> str:
        """Extract visible body text from the page."""
        try:
            text = self.driver.execute_script(
                "return document.body ? document.body.innerText : '';"
            )
            return text or ""
        except Exception:
            return ""

    def next_page_url(self) -> str | None:
        """Find the next-page URL from rel=next links, rejecting cross-origin."""
        try:
            elements = self.driver.find_elements(By.CSS_SELECTOR, "a[rel='next']")
            for el in elements:
                href = el.get_attribute("href")
                if href:
                    try:
                        validated = self._validated_navigation_url(href)
                        return validated
                    except BrowserNavigationError:
                        continue
        except Exception:
            pass
        return None

    def get_project_page(self, url: str) -> str:
        """Navigate to a project detail page and return the page source HTML."""
        validated = self._validated_navigation_url(url)
        if not re.search(r"/project/[^/?#]+", urlparse(validated).path):
            raise BrowserNavigationError("URL is not a project detail page")
        self.driver.get(validated)
        self._wait_for_page_load()
        return self.get_page_source()

    def get(self, url: str) -> str:
        """Navigate to a URL and return the page source HTML."""
        validated = self._validated_navigation_url(url)
        self.driver.get(validated)
        self._wait_for_page_load()
        return self.get_page_source()

    def load_listing_page(self, url: str) -> str:
        """Navigate to a listing page and return its page source HTML."""
        validated = self._validated_navigation_url(url)
        self.driver.get(validated)
        self._wait_for_page_load()
        return self.get_page_source()

    def is_logged_in(self) -> bool:
        """Return True if the browser session appears authenticated.

        Checks whether the current page is NOT a login page and the
        session has navigated past the login gate at least once.
        """
        try:
            current = self.driver.current_url or ""
            if _is_login_url(current):
                return False
            state = self._page_state()
            if state.login_required or state.challenge_detected or state.error_detected:
                return False
            body = self._body_text()
            if not body.strip():
                return False
            login_indicators = re.compile(
                r"(?:log\s*in|sign\s*in|anmelden|registrieren)",
                re.I,
            )
            if login_indicators.search(body[:500]):
                return False
            return state.ready
        except Exception:
            return False

    def interactive_login(self, timeout_seconds: int = 600) -> bool:
        """Open a visible browser and wait for the user to log in manually.

        Returns True if login was detected before the timeout.
        """
        try:
            login_url = getattr(Config, "LOGIN_URL", None)
            if not login_url:
                return False
            validated = self._validated_navigation_url(login_url)
            self.driver.get(validated)
            self._wait_for_page_load()
            deadline = time.monotonic() + timeout_seconds
            poll_interval = 2.0
            while time.monotonic() < deadline:
                if self.is_logged_in():
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(poll_interval, remaining))
            return self.is_logged_in()
        except Exception as exc:
            LOGGER.warning("Interactive login failed: %s", exc)
            return False

    def login_with_credentials(self) -> bool:
        """Attempt to log in using credentials from Config.

        Returns True if login succeeded.
        """
        email = getattr(Config, "LOGIN_EMAIL", "") or ""
        password = getattr(Config, "LOGIN_PASSWORD", "") or ""
        if not email or not password:
            LOGGER.warning("LOGIN_EMAIL and LOGIN_PASSWORD must be set for credential login.")
            return False
        try:
            login_url = getattr(Config, "LOGIN_URL", None)
            if not login_url:
                return False
            validated = self._validated_navigation_url(login_url)
            self.driver.get(validated)
            self._wait_for_page_load()
            if self.is_logged_in():
                return True
            email_inputs = self.driver.find_elements(
                By.CSS_SELECTOR,
                "input[type='email'], input[name='email'], input[id*='email'], input[id*='login']",
            )
            password_inputs = self.driver.find_elements(
                By.CSS_SELECTOR,
                "input[type='password']",
            )
            if not email_inputs or not password_inputs:
                LOGGER.warning("Could not locate login form fields on %s", validated)
                return False
            email_field = email_inputs[0]
            email_field.clear()
            email_field.send_keys(email)
            password_field = password_inputs[0]
            password_field.clear()
            password_field.send_keys(password)
            submit_buttons = self.driver.find_elements(
                By.CSS_SELECTOR,
                "button[type='submit'], input[type='submit'], button[name='submit']",
            )
            if submit_buttons:
                submit_buttons[0].click()
            else:
                password_field.submit()
            self._wait_for_page_load()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if self.is_logged_in():
                    return True
                time.sleep(2)
            return self.is_logged_in()
        except Exception as exc:
            LOGGER.warning("Credential login failed: %s", exc)
            return False

    def _wait_for_page_load(self, timeout: float | None = None) -> None:
        """Wait for the page to reach a ready state."""
        timeout = timeout or Config.PAGE_LOAD_TIMEOUT
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                state = self.driver.execute_script("return document.readyState")
                if state == "complete":
                    return
            except Exception:
                pass
            time.sleep(0.5)

    def scroll_to_bottom(self, max_scrolls: int | None = None) -> None:
        """Scroll to page bottom to trigger lazy-loaded content."""
        max_scrolls = max_scrolls or Config.MAX_SCROLLS_PER_PAGE
        for _ in range(max_scrolls):
            try:
                prev_height = self.driver.execute_script(
                    "return Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);"
                )
                self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(Config.SCROLL_PAUSE_SECONDS)
                new_height = self.driver.execute_script(
                    "return Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);"
                )
                if new_height == prev_height:
                    break
            except Exception:
                break

    def get_page_source(self) -> str:
        """Return the current page source."""
        try:
            return self.driver.page_source or ""
        except Exception:
            return ""

    def current_url(self) -> str:
        """Return the current browser URL."""
        try:
            return self.driver.current_url or ""
        except Exception:
            return ""

    def navigate(self, url: str) -> None:
        """Navigate to a validated URL."""
        validated = self._validated_navigation_url(url)
        self.driver.get(validated)
        self._wait_for_page_load()
