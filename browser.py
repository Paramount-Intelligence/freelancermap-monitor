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

from config import Config, _is_feed_listing_path
from pagedetect import (
    ERROR_BODY_RE,
    ERROR_TITLE_RE,
    detect_challenge,
    detect_error,
    has_error_title,
    is_login_url,
)


# Backwards-compatible alias for existing imports and call sites.
_is_login_url = is_login_url


LOGGER = logging.getLogger(__name__)


class BrowserNavigationError(Exception):
    """Raised when a navigation target fails origin or safety checks."""


class PageLoadTimeoutError(Exception):
    """Raised when a page never reached a usable ready state in time.

    The message carries the requested or final URL (never the page HTML)
    and the timeout duration so failures close safely instead of parsing
    partial or unloaded content.
    """


@dataclass
class PageState:
    """Snapshot of the current browser page classification."""

    ready: bool = False
    login_required: bool = False
    challenge_detected: bool = False
    error_detected: bool = False
    empty: bool = False


from dataclasses import dataclass
from utils import exclusive_file_lock, canonicalize_url, polite_sleep, utc_now_iso


class BrowserProfileInUseError(Exception):
    """Raised when the persistent Chrome profile is locked by another process."""


@dataclass
class AuthVerificationResult:
    """Detailed result of authenticating the current browser session."""
    authenticated: bool = False
    reason: str = ""


class PageNotFoundError(Exception):
    """Raised when a 404 or 410 Not Found page is encountered."""


class HttpError(Exception):
    """Raised when an HTTP error status or page error is encountered."""


PROJECT_PATH_RE = re.compile(r"^/project/[^/?#]+$", re.I)


class BrowserSession:
    """Selenium Chrome session with origin-safe navigation guards."""

    def __init__(
        self,
        headless: bool | None = None,
        driver_factory: type | None = None,
        autostart: bool = True,
    ) -> None:
        if headless is None:
            self.headless = bool(getattr(Config, "HEADLESS", True))
        else:
            self.headless = bool(headless)
        self.driver_factory = driver_factory
        self.driver: WebDriver | None = None
        self._profile_lock_context = None
        if autostart:
            self._ensure_driver()

    def _ensure_driver(self) -> None:
        if self.driver is not None:
            return

        profile_dir = getattr(Config, "CHROME_PROFILE_DIR", None)
        if profile_dir and not self.driver_factory:
            lock_file = profile_dir.parent / "chrome_profile.lock"
            try:
                self._profile_lock_context = exclusive_file_lock(lock_file, timeout_seconds=1.0)
                self._profile_lock_context.__enter__()
            except Exception as exc:
                raise BrowserProfileInUseError(
                    f"Chrome profile at {profile_dir} is already locked by another process."
                ) from exc

        options = Options()
        if self.headless:
            options.add_argument("--headless=new")
        if bool(getattr(Config, "CHROME_NO_SANDBOX", False)):
            options.add_argument("--no-sandbox")
        if bool(getattr(Config, "CHROME_DISABLE_DEV_SHM_USAGE", False)):
            options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1920,1080")
        
        # Configure persistent Chrome profile directory and profile name
        if profile_dir:
            profile_path = profile_dir.resolve()
            profile_path.mkdir(parents=True, exist_ok=True)
            options.add_argument(f"--user-data-dir={profile_path}")
            profile_name = getattr(Config, "CHROME_PROFILE_NAME", "Default")
            if profile_name:
                options.add_argument(f"--profile-directory={profile_name}")

        try:
            if self.driver_factory:
                self.driver = self.driver_factory(options)
            else:
                self.driver = webdriver.Chrome(options=options)
        except Exception:
            if self._profile_lock_context is not None:
                try:
                    self._profile_lock_context.__exit__(None, None, None)
                except Exception:
                    pass
                self._profile_lock_context = None
            raise
        
        page_load_timeout = getattr(Config, "PAGE_LOAD_TIMEOUT", 30)
        script_timeout = getattr(Config, "SCRIPT_TIMEOUT_SECONDS", 15)
        self.driver.set_page_load_timeout(page_load_timeout)
        self.driver.set_script_timeout(script_timeout)
        self.driver.implicitly_wait(getattr(Config, "IMPLICIT_WAIT_SECONDS", 3))

    def close(self) -> None:
        """Quit the underlying driver and release profile locks."""
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None
        if self._profile_lock_context is not None:
            try:
                self._profile_lock_context.__exit__(None, None, None)
            except Exception:
                pass
            self._profile_lock_context = None

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

            # Challenge detection uses shared pagedetect semantics: a lone
            # recaptcha script tag or "please wait" is never a challenge.
            challenge = detect_challenge(title, body_text, page_source)

            error = detect_error(title, body_text)

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

    def accept_cookie_banner(self) -> None:
        """Attempt to accept cookie consent banners if present.

        Only accept buttons inside an explicit consent container (a cookie
        consent, OneTrust, or GDPR banner) are clicked.  An unrelated page
        button whose class or id merely contains "accept" or "consent"
        (e.g. an "Accept offer" button) is never touched.
        """
        if not self.driver:
            return
        container_selectors = (
            "#onetrust-banner-sdk",
            "[id*='onetrust']",
            "[class*='onetrust']",
            "[class*='cookie-consent']",
            "[class*='cookieConsent']",
            "[class*='consent-banner']",
            "[class*='consentBanner']",
            "[class*='cc-banner']",
            "[class*='cookie']",
            "[id*='cookie-banner']",
            "[class*='gdpr']",
            "[data-testid='cookie-banner']",
        )
        accept_words = (
            "accept",
            "agree",
            "allow",
            "consent",
            "confirm",
            "zustimmen",
            "einverstanden",
            "ok",
        )
        try:
            for selector in container_selectors:
                containers = self.driver.find_elements(By.CSS_SELECTOR, selector)
                for container in containers:
                    if not container.is_displayed():
                        continue
                    try:
                        buttons = container.find_elements(
                            By.CSS_SELECTOR, "button, a[role='button'], a"
                        )
                    except Exception:
                        buttons = []
                    for button in buttons:
                        try:
                            if not button.is_displayed():
                                continue
                            label = (
                                (button.get_attribute("aria-label") or "")
                                + " "
                                + (button.text or "")
                            ).casefold()
                            if any(word in label for word in accept_words):
                                button.click()
                                time.sleep(0.5)
                                return
                        except Exception:
                            continue
        except Exception:
            return

    def get_project_page(self, url: str) -> str:
        """Navigate to a project detail page and return the page source HTML."""
        validated = self._validated_navigation_url(url)
        path = urlparse(validated).path
        if not PROJECT_PATH_RE.fullmatch(path) and not re.search(r"/project/[^/?#]+$", path):
            raise BrowserNavigationError("URL is not a project detail page")
        project_key = path.rstrip("/").rsplit("/", 1)[-1]
        self.driver.get(validated)
        self._require_usable_page(
            expected_kind="detail",
            requested_url=url,
            expected_project_key=project_key,
        )
        self.accept_cookie_banner()
        return self.get_page_source()

    def get(self, url: str) -> str:
        """Navigate to a URL and return the page source HTML."""
        validated = self._validated_navigation_url(url)
        self.driver.get(validated)
        self._require_usable_page(
            expected_kind="generic",
            requested_url=url,
        )
        self.accept_cookie_banner()
        return self.get_page_source()

    def _current_sort_state(self) -> str | None:
        """Read the active sort value from the listing DOM, if present.

        Freelancermap renders the sort state as a checked radio
        (``input[name='sort-option']:checked``) and an active dropdown entry
        (``li[data-value].active``). A missing control yields None and must not
        be treated as proof of any particular sort.
        """
        try:
            radios = self.driver.find_elements(
                By.CSS_SELECTOR,
                "input[name='sort-option']:checked",
            )
            for radio in radios:
                value = radio.get_attribute("value")
                if value:
                    return value
            active = self.driver.find_elements(
                By.CSS_SELECTOR,
                "li[data-value].active, [role='button'][data-value].active",
            )
            for element in active:
                value = element.get_attribute("data-value")
                if value:
                    return value
        except Exception:
            pass
        return None

    def _visible_loading_indicator(self) -> bool:
        """True when any loading indicator is currently visible in the DOM.

        Only ``is_displayed()`` elements count: hidden spinners, skeleton
        templates, or CSS-only loaders that are not rendered must never
        block an otherwise usable page.
        """
        selectors = (
            ".loading",
            ".spinner",
            ".is-loading",
            "[aria-busy='true']",
            ".skeleton",
        )
        for selector in selectors:
            try:
                for element in self.driver.find_elements(By.CSS_SELECTOR, selector):
                    try:
                        if element.is_displayed():
                            return True
                    except Exception:
                        continue
            except Exception:
                continue
        return False

    def _load_more_available(self) -> bool:
        """True when a legitimate, visible load-more control is present."""
        if not self.driver:
            return False
        try:
            buttons = self.driver.find_elements(
                By.CSS_SELECTOR,
                "a.btn-load-more, button.btn-load-more, .load-more-button, a[data-action='load-more']",
            )
            for btn in buttons:
                try:
                    if btn.is_displayed():
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    def _document_height(self) -> int:
        """Return the current rendered document height, or 0 on failure."""
        try:
            value = self.driver.execute_script(
                "return Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);"
            )
            return int(value or 0)
        except Exception:
            return 0

    def _listing_snapshot(self) -> tuple[int, int, bool, bool]:
        """Snapshot the listing state for stability detection.

        A stable listing requires the project-route count, the document
        height, the visible loading indicators, and the load-more
        availability to all be unchanged for the configured number of
        consecutive rounds.
        """
        return (
            self._project_route_count(),
            self._document_height(),
            self._visible_loading_indicator(),
            self._load_more_available(),
        )

    def _wait_for_listing_stability(
        self,
        *,
        timeout: float | None = None,
    ) -> None:
        """Wait for the listing to remain stable for LISTING_STABLE_ROUNDS.

        Raises PageLoadTimeoutError when the deadline passes first, so a
        constantly-growing or never-settling listing never gets parsed
        while it is still loading.
        """
        required = max(1, int(getattr(Config, "LISTING_STABLE_ROUNDS", 2)))
        timeout = timeout or float(getattr(Config, "PAGE_LOAD_TIMEOUT", 30))
        poll = max(
            0.01,
            float(getattr(Config, "LISTING_STABILITY_POLL_SECONDS", 0.5)),
        )
        deadline = time.monotonic() + timeout
        stable_rounds = 0
        previous: tuple[int, int, bool, bool] | None = None
        while time.monotonic() < deadline:
            snapshot = self._listing_snapshot()
            if previous is not None and snapshot == previous:
                stable_rounds += 1
            else:
                stable_rounds = 0
            previous = snapshot
            if stable_rounds >= required:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(poll, remaining))
        raise PageLoadTimeoutError(
            "Listing did not stabilize within "
            f"{timeout:.0f}s ({required} consecutive stable round(s) "
            "never observed)."
        )

    def _wait_for_loading_finished(
        self,
        timeout: float | None = None,
    ) -> bool:
        """Wait until no loading indicator is visible in the listing DOM.

        Returns True when the listing is free of visible loading
        indicators, and False if the deadline passed with an indicator
        still displayed. Callers must treat False as a timeout and never
        silently continue parsing partial content.
        """
        timeout = timeout or float(getattr(Config, "PAGE_LOAD_TIMEOUT", 30))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._visible_loading_indicator():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.5, remaining))
        return not self._visible_loading_indicator()

    def _current_url_sort_value(self) -> str | None:
        """Read the configured sort parameter from the final URL, if present."""
        param = str(getattr(Config, "FEED_QUERY_SORT_PARAM", "sort"))
        try:
            parsed = urlparse(self.driver.current_url or "")
            for part in (parsed.query or "").split("&"):
                name, _, value = part.partition("=")
                if name.casefold() == param.casefold():
                    return value or None
        except Exception:
            pass
        return None

    def _verify_listing_sort(self, expected_sort: str) -> None:
        """Fail closed unless the final URL or a rendered control proves the sort.

        The final URL is proven by the configured sort parameter in the
        final URL; the rendered control is the checked sort radio or the
        active dropdown entry. A conflict from either source raises;
        missing proof from both sources raises as well.
        """
        url_sort = self._current_url_sort_value()
        dom_sort = self._current_sort_state()
        current = self.driver.current_url or "(unknown URL)"

        if url_sort is not None and url_sort != expected_sort:
            raise HttpError(
                f"Listing page {current} is sorted {url_sort!r} per its "
                f"final URL, expected {expected_sort!r}. Refusing to scan a "
                "differently sorted feed."
            )
        if dom_sort is not None and dom_sort != expected_sort:
            raise HttpError(
                f"Listing page {current} renders sort {dom_sort!r}, "
                f"expected {expected_sort!r}. Refusing to scan a differently "
                "sorted feed."
            )
        if url_sort != expected_sort and dom_sort != expected_sort:
            raise HttpError(
                f"Listing page {current} neither carries sort={expected_sort} "
                "in its final URL nor renders a control proving the expected "
                "sort value. Refusing to scan an unverifiable feed."
            )

    def _visible_password_form(self) -> bool:
        """True when a visible password input is present in the DOM."""
        try:
            for element in self.driver.find_elements(
                By.CSS_SELECTOR,
                "input[type='password']",
            ):
                try:
                    if element.is_displayed():
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    def _require_usable_page(
        self,
        *,
        expected_kind: str,
        requested_url: str = "",
        expected_project_key: str = "",
        expected_sort: str | None = None,
    ) -> None:
        """Fail-closed post-navigation boundary applied after every driver.get().

        Validates document readiness, a non-empty usable body, an HTTPS
        same-origin final URL free of credentials and control characters,
        the expected route type, the absence of login redirects, active
        password forms, CAPTCHA/MFA challenges, and HTTP error pages, the
        exact project identity on detail pages, and listing route/sort
        preservation on listing pages. Any violation raises instead of
        returning partial HTML to a parser.
        """
        self._wait_for_page_load(requested_url=requested_url)

        current = self.driver.current_url or ""
        parsed = urlparse(current)

        if parsed.username is not None or parsed.password is not None:
            raise BrowserNavigationError(
                "Final URL must not contain embedded credentials"
            )
        if any(character in current for character in ("\n", "\r", "\x00")):
            raise BrowserNavigationError("Final URL contains control characters")
        scheme = parsed.scheme.casefold()
        if scheme not in {"http", "https"}:
            raise BrowserNavigationError(
                f"Final URL has an unsupported scheme: {scheme or '(none)'}"
            )
        if scheme == "http" and not Config.ALLOW_INSECURE_HTTP:
            raise BrowserNavigationError(
                "Final URL is not HTTPS; insecure navigation is not allowed"
            )
        if not self._same_origin(current, Config.BASE_URL):
            raise BrowserNavigationError(
                "Final URL left the configured origin"
            )

        body_text = self._body_text()
        if not self._is_page_ready(body_text):
            raise PageLoadTimeoutError(
                "Document never reached a ready state on "
                f"{requested_url or current} within "
                f"{int(getattr(Config, 'PAGE_LOAD_TIMEOUT', 30))}s."
            )

        state = self._page_state()
        if state.empty:
            raise HttpError(
                "The page body is empty after navigating to "
                f"{requested_url or current}; refusing to parse partial HTML."
            )
        if state.error_detected:
            title = (self.driver.title or "").strip()
            if has_error_title(title):
                raise PageNotFoundError(
                    f"Page {requested_url or current} returned "
                    f"404/Error: '{title}'"
                )
            raise HttpError(
                f"Page {requested_url or current} returned an error "
                f"state: '{title}'"
            )
        if state.challenge_detected:
            raise HttpError(
                "A CAPTCHA, bot check, or MFA challenge page was encountered "
                "after navigating to "
                f"{requested_url or current}; refusing to continue."
            )
        if _is_login_url(current):
            raise HttpError(
                "Navigated to a login page when protected content was "
                f"expected: {current}"
            )
        if self._visible_password_form():
            raise HttpError(
                "A password form is still active on "
                f"{requested_url or current}; the session is not "
                "authenticated."
            )

        path = parsed.path or "/"
        if expected_kind == "detail":
            if not PROJECT_PATH_RE.fullmatch(path) and not re.search(
                r"/project/[^/?#]+$",
                path,
            ):
                raise BrowserNavigationError(
                    "Final URL is not a project detail page"
                )
            if expected_project_key and (
                path.rstrip("/").casefold()
                != f"/project/{expected_project_key}".casefold()
            ):
                raise HttpError(
                    f"Final URL {current} does not match the requested "
                    f"project {expected_project_key!r}."
                )
        elif expected_kind == "listing":
            if not _is_feed_listing_path(path):
                raise BrowserNavigationError(
                    "Final URL is not a listing/search route: "
                    f"{current}"
                )
            if expected_sort is not None:
                self._verify_listing_sort(expected_sort)

    def load_listing_page(
        self,
        url: str,
        *,
        expected_sort: str | None = None,
    ) -> str:
        """Navigate to a listing page, verify sort state, and load dynamic content.

        Scrolling uses ``Config.MAX_SCROLLS_PER_PAGE``, load-more buttons are
        clicked at most ``Config.MAX_LOAD_MORE_CLICKS`` times, and every
        phase waits for ``Config.LISTING_STABLE_ROUNDS`` consecutive stable
        rounds (project-route count, document height, visible loading
        indicators, and load-more availability) before continuing. Any
        timeout raises instead of parsing a page that is still loading.
        """
        validated = self._validated_navigation_url(url)
        self.driver.get(validated)
        self._require_usable_page(
            expected_kind="listing",
            requested_url=url,
            expected_sort=expected_sort,
        )
        self.accept_cookie_banner()
        self._wait_for_listing_stability()

        # Scroll to load lazy content using the configured per-page scroll cap.
        max_scrolls = int(getattr(Config, "MAX_SCROLLS_PER_PAGE", 6))
        if max_scrolls > 0:
            self.scroll_to_bottom(max_scrolls=max_scrolls)
        self._wait_for_listing_stability()

        # Click 'load more' repeatedly, waiting for content to stabilise.
        max_clicks = int(getattr(Config, "MAX_LOAD_MORE_CLICKS", 3))
        if max_clicks > 0:
            for _ in range(max_clicks):
                before = self._project_route_count()
                if not self.click_load_more():
                    break
                if not self._wait_for_loading_finished():
                    raise PageLoadTimeoutError(
                        "Loading indicators never cleared after a load-more "
                        "click within "
                        f"{int(getattr(Config, 'PAGE_LOAD_TIMEOUT', 30))}s."
                    )
                self._wait_for_listing_stability()
                after = self._project_route_count()
                if after <= before:
                    break

        return self.get_page_source()

    def verify_authenticated_session(self) -> AuthVerificationResult:
        """Navigate to ACCOUNT_URL and verify if session is authenticated.

        The shared post-navigation boundary runs first (readiness, HTTPS
        same-origin final URL, no error/challenge/login/password pages).
        Any violation is converted into an unauthenticated result. Only a
        positive visible authenticated marker -- a logout link, the user
        menu/avatar, or an account-dashboard phrase -- counts as success.
        """
        try:
            account_url = getattr(Config, "ACCOUNT_URL", "https://www.freelancermap.com/my_account.html")
            validated = self._validated_navigation_url(account_url)
            self.driver.get(validated)
            self._require_usable_page(
                expected_kind="account",
                requested_url=account_url,
            )
            self.accept_cookie_banner()

            current = self.driver.current_url or ""
            if _is_login_url(current):
                return AuthVerificationResult(authenticated=False, reason=f"Redirected to login page: {current}")

            # Verify POSITIVE authenticated DOM markers (URL ALONE DOES NOT PROVE AUTHENTICATION)
            body = self._body_text().casefold()
            logout_elements = self.driver.find_elements(By.CSS_SELECTOR, "a[href*='logout'], a[href*='abmelden'], [data-id='user-menu'], .user-profile, [data-testid='user-avatar']")
            if logout_elements or "logout" in body or "abmelden" in body or "my freelancermap" in body or "account dashboard" in body:
                return AuthVerificationResult(authenticated=True, reason="Positive authenticated DOM marker verified")

            return AuthVerificationResult(authenticated=False, reason="Could not verify positive authenticated DOM markers on account page")
        except Exception as exc:
            return AuthVerificationResult(authenticated=False, reason=f"Authentication check failed with error: {exc}")

    def _apparently_past_login_gate(self) -> bool:
        """Light, non-authoritative signal that the browser left the login form.

        Never treated as proof of authentication. It only decides when a
        strong ACCOUNT_URL verification is worth performing so manual
        login flows are not disturbed while the form is still displayed.
        """
        try:
            if _is_login_url(self.driver.current_url or ""):
                return False
            state = self._page_state()
            if state.login_required or state.challenge_detected:
                return False
            body = self._body_text()
            if not body.strip():
                return False
            return state.ready
        except Exception:
            return False

    def _confirm_authenticated(self) -> bool:
        """Authoritative post-login verification used by the login flows.

        Navigates to Config.ACCOUNT_URL, runs verify_authenticated_session(),
        and requires a positive visible authenticated marker. A login
        redirect, password form, CAPTCHA/MFA challenge, HTTP error, or a
        missing account marker all return False.
        """
        return self.verify_authenticated_session().authenticated

    def is_logged_in(self) -> bool:
        """Return True if the browser session *appears* authenticated.

        NON-AUTHORITATIVE: this heuristic only checks the current page and
        never navigates to the account route, so it must not be used as the
        success condition of any login flow. Use
        :meth:`verify_authenticated_session` (or :meth:`_confirm_authenticated`)
        for authentication-sensitive decisions.
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

        Once the browser appears to have left the login form, the session
        is verified authoritatively: navigate to Config.ACCOUNT_URL and
        require a positive authenticated marker via
        verify_authenticated_session(). Returns True only when that strong
        check passes before the timeout.
        """
        try:
            login_url = getattr(Config, "LOGIN_URL", None)
            if not login_url:
                return False
            validated = self._validated_navigation_url(login_url)
            self.driver.get(validated)
            self._wait_for_page_load(requested_url=login_url)
            deadline = time.monotonic() + timeout_seconds
            poll_interval = 2.0
            while time.monotonic() < deadline:
                if self._apparently_past_login_gate():
                    if self._confirm_authenticated():
                        return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(poll_interval, remaining))
            return self._confirm_authenticated()
        except Exception as exc:
            LOGGER.warning("Interactive login failed: %s", exc)
            return False

    def login_with_credentials(self) -> bool:
        """Attempt to log in using credentials from Config.

        The success condition is the authoritative account verification
        (navigate to ACCOUNT_URL and require a positive authenticated
        marker), never the page-local is_logged_in() heuristic.

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
            self._wait_for_page_load(requested_url=login_url)
            if not _is_login_url(self.driver.current_url or ""):
                if self._confirm_authenticated():
                    return True
                self.driver.get(validated)
                self._wait_for_page_load(requested_url=login_url)
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
            password_field = password_inputs[0]
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    if email_field.is_enabled() and email_field.is_displayed() and password_field.is_enabled() and password_field.is_displayed():
                        break
                except Exception:
                    pass
                time.sleep(0.5)
            else:
                LOGGER.warning("Login form fields never became interactable on %s", validated)
                return False
            email_field.click()
            email_field.clear()
            email_field.send_keys(email)
            password_field.click()
            password_field.clear()
            password_field.send_keys(password)
            submit_button = None
            submit_buttons = self.driver.find_elements(
                By.CSS_SELECTOR,
                "button[type='submit'], input[type='submit'], button[name='submit']",
            )
            for candidate in submit_buttons:
                try:
                    if candidate.is_displayed() and candidate.is_enabled():
                        submit_button = candidate
                        break
                except Exception:
                    continue
            if submit_button is None:
                labeled_buttons = self.driver.find_elements(By.CSS_SELECTOR, "button")
                for candidate in labeled_buttons:
                    try:
                        label = candidate.text.strip().casefold()
                        clickable = candidate.is_displayed() and candidate.is_enabled()
                    except Exception:
                        continue
                    if clickable and label in ("log in", "login", "sign in", "anmelden"):
                        submit_button = candidate
                        break
            if submit_button is not None:
                submit_button.click()
            else:
                password_field.submit()
            self._wait_for_page_load(requested_url=login_url)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if self._confirm_authenticated():
                    return True
                time.sleep(2)
            return self._confirm_authenticated()
        except Exception as exc:
            LOGGER.warning("Credential login failed: %s", exc)
            return False

    def _wait_for_page_load(
        self,
        timeout: float | None = None,
        *,
        requested_url: str = "",
    ) -> None:
        """Wait for the page to reach a ready state.

        Raises PageLoadTimeoutError when document.readyState never becomes
        ``complete`` (including when execute_script keeps raising) before
        the configured timeout. The message includes the requested or
        final URL and the timeout duration, never the page HTML or any
        credentials.
        """
        timeout = timeout or Config.PAGE_LOAD_TIMEOUT
        deadline = time.monotonic() + timeout
        last_state: str | None = None
        while time.monotonic() < deadline:
            try:
                state = self.driver.execute_script("return document.readyState")
                last_state = str(state or "")
                if last_state == "complete":
                    return
            except Exception:
                last_state = None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.5, remaining))

        location = requested_url
        if not location:
            try:
                location = self.driver.current_url or ""
            except Exception:
                location = ""
        raise PageLoadTimeoutError(
            "Page did not reach a ready state "
            f"(readyState={last_state or 'unknown'}) within "
            f"{timeout:.0f}s: {location or '(URL unavailable)'}"
        )

    def scroll_to_bottom(self, max_scrolls: int | None = None) -> None:
        """Scroll to page bottom to trigger lazy-loaded content.

        Scrolling continues while the document height, the project-route
        count, or the visible loading indicators are still changing. It
        stops only when all three are unchanged, so a single unchanged
        height measurement never truncates a listing that is still
        growing.
        """
        max_scrolls = max_scrolls or Config.MAX_SCROLLS_PER_PAGE
        previous_route_count = self._project_route_count()
        for _ in range(max_scrolls):
            try:
                prev_height = self._document_height()
                self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(Config.SCROLL_PAUSE_SECONDS)
                new_height = self._document_height()
                route_count = self._project_route_count()
                loader_visible = self._visible_loading_indicator()
                height_unchanged = new_height == prev_height
                routes_stable = route_count == previous_route_count
                if height_unchanged and routes_stable and not loader_visible:
                    break
                previous_route_count = route_count
            except Exception:
                break

    def click_load_more(self) -> bool:
        """Click load-more button if present on listing page."""
        if not self.driver:
            return False
        try:
            buttons = self.driver.find_elements(
                By.CSS_SELECTOR,
                "a.btn-load-more, button.btn-load-more, .load-more-button, a[data-action='load-more']"
            )
            for btn in buttons:
                if btn.is_displayed():
                    btn.click()
                    time.sleep(1.0)
                    return True
        except Exception:
            pass
        return False

    def get_page_source(self) -> str:
        """Return the current page source."""
        if not self.driver:
            return ""
        try:
            source = getattr(self.driver, "page_source", "")
            if callable(source):
                source = source()
            if isinstance(source, (str, bytes)):
                return source if isinstance(source, str) else source.decode("utf-8", errors="replace")
            return ""
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
        self._require_usable_page(
            expected_kind="generic",
            requested_url=url,
        )
