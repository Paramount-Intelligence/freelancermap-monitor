from __future__ import annotations

import os
import re
from email.utils import parseaddr
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


# Feed URLs must be listing/search routes. Detail, authentication, account, and
# application routes are rejected so a cycle can never silently scan the wrong
# page type.
FEED_LISTING_BANNED_PATH_PREFIXES = (
    "/project/",
    "/login",
    "/sign-in",
    "/signin",
    "/registration",
    "/logout",
    "/my_account",
    "/account",
    "/dashboard",
    "/app/",
    "/email-login",
    "/password-request",
)


def _query_param_value(parsed: Any, key: str) -> str | None:
    """Return the first query parameter value for *key*, or None."""
    for part in (parsed.query or "").split("&"):
        name, _, value = part.partition("=")
        if name.casefold() == key.casefold():
            return value or ""
    return None


def _is_feed_listing_path(path: str) -> bool:
    """True when *path* looks like a project listing/search route."""
    lowered = (path or "/").rstrip("/").casefold()
    for prefix in FEED_LISTING_BANNED_PATH_PREFIXES:
        if lowered.startswith(prefix):
            return False
    return True


ROOT_DIR = Path(__file__).resolve().parent
load_dotenv(ROOT_DIR / ".env", override=False)


_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}
_EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)

    if raw is None or not raw.strip():
        return default

    value = raw.strip().casefold()

    if value in _TRUE_VALUES:
        return True

    if value in _FALSE_VALUES:
        return False

    raise RuntimeError(
        f"{name} must be one of true/false, yes/no, on/off, or 1/0; "
        f"got {raw!r}."
    )


def _env_int(
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    raw = os.getenv(name, str(default)).strip()

    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"{name} must be an integer, got {raw!r}."
        ) from exc

    if minimum is not None and value < minimum:
        raise RuntimeError(
            f"{name} must be at least {minimum}, got {value}."
        )

    if maximum is not None and value > maximum:
        raise RuntimeError(
            f"{name} must be at most {maximum}, got {value}."
        )

    return value


def _env_float(
    name: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    raw = os.getenv(name, str(default)).strip()

    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"{name} must be numeric, got {raw!r}."
        ) from exc

    if minimum is not None and value < minimum:
        raise RuntimeError(
            f"{name} must be at least {minimum}, got {value}."
        )

    if maximum is not None and value > maximum:
        raise RuntimeError(
            f"{name} must be at most {maximum}, got {value}."
        )

    return value


def _env_path(name: str, default: Path) -> Path:
    """
    Read a filesystem path from the environment.

    Relative paths are resolved from the repository directory instead of the
    shell's working directory. This keeps paths deterministic when the monitor
    is launched through Task Scheduler, Docker, Railway, systemd, or PowerShell.
    """

    raw = os.getenv(name)

    if raw and raw.strip():
        candidate = Path(raw.strip()).expanduser()
    else:
        candidate = default

    if not candidate.is_absolute():
        candidate = ROOT_DIR / candidate

    return candidate.resolve(strict=False)


def _env_csv(name: str, default: str = "") -> list[str]:
    """
    Parse a comma-separated environment variable.

    Blank values are removed and duplicate values are eliminated
    case-insensitively while preserving the original order.
    """

    raw = os.getenv(name, default)

    values: list[str] = []
    seen: set[str] = set()

    for item in raw.split(","):
        value = item.strip()

        if not value:
            continue

        key = value.casefold()

        if key in seen:
            continue

        values.append(value)
        seen.add(key)

    return values


def _valid_email_address(value: str) -> bool:
    """
    Validate one bare email address.

    Display-name formats such as ``Name <name@example.com>`` are intentionally
    rejected in environment variables. The display name belongs in
    SMTP_FROM_NAME and is encoded safely by email.utils.formataddr.
    """

    if not value:
        return False

    if "\r" in value or "\n" in value:
        return False

    display_name, address = parseaddr(value)

    if display_name and address != value:
        return False

    return bool(_EMAIL_RE.fullmatch(address))


def _url_origin(value: str) -> tuple[str, str, int | None]:
    parsed = urlparse(value)

    return (
        parsed.scheme.casefold(),
        (parsed.hostname or "").casefold(),
        parsed.port,
    )


def _contains_header_injection(value: str) -> bool:
    return "\r" in value or "\n" in value


class Config:
    """
    Environment-backed application configuration.

    The project intentionally keeps the ``Config.NAME`` interface because the
    rest of the monitor and its tests use class attributes and patch individual
    values during tests.

    Every numeric and Boolean environment value is parsed and bounded when this
    module is imported. Invalid deployment configuration therefore fails early
    instead of failing halfway through a scan.
    """

    ROOT_DIR = ROOT_DIR

    # -------------------------------------------------------------------------
    # Local storage
    # -------------------------------------------------------------------------

    DATA_DIR = _env_path(
        "DATA_DIR",
        ROOT_DIR / "data",
    )

    DATABASE_PATH = _env_path(
        "DATABASE_PATH",
        DATA_DIR / "freelancermap_projects.db",
    )

    CHROME_PROFILE_DIR = _env_path(
        "CHROME_PROFILE_DIR",
        DATA_DIR / "chrome_profile",
    )

    LOCK_PATH = _env_path(
        "LOCK_PATH",
        DATA_DIR / "freelancermap_monitor.lock",
    )

    DIAGNOSTICS_DIR = _env_path(
        "DIAGNOSTICS_DIR",
        DATA_DIR / "diagnostics",
    )

    HEARTBEAT_PATH = _env_path(
        "HEARTBEAT_PATH",
        DATA_DIR / "heartbeat.json",
    )

    # -------------------------------------------------------------------------
    # Freelancermap endpoints and authentication
    # -------------------------------------------------------------------------

    CHROME_PROFILE_NAME = os.getenv(
        "CHROME_PROFILE_NAME",
        "Default",
    ).strip() or "Default"

    BASE_URL = os.getenv(
        "FREELANCERMAP_BASE_URL",
        "https://www.freelancermap.com",
    ).strip().rstrip("/")

    ACCOUNT_URL = os.getenv(
        "FREELANCERMAP_ACCOUNT_URL",
        f"{BASE_URL}/my_account.html",
    ).strip()

    PROJECTS_URL = os.getenv(
        "FREELANCERMAP_PROJECTS_URL",
        f"{BASE_URL}/projects",
    ).strip()

    LOGIN_URL = os.getenv(
        "FREELANCERMAP_LOGIN_URL",
        f"{BASE_URL}/login",
    ).strip()

    PRIMARY_SEARCH_URL = os.getenv(
        "FREELANCERMAP_PRIMARY_SEARCH_URL",
        PROJECTS_URL,
    ).strip() or PROJECTS_URL

    PERSONALIZED_SEARCH_URL = os.getenv(
        "FREELANCERMAP_PERSONALIZED_SEARCH_URL",
        "",
    ).strip()

    ENABLE_PERSONALIZED_FEED = _env_bool(
        "ENABLE_PERSONALIZED_FEED",
        False,
    )

    # When true, a failing or unreachable personalized feed fails the whole
    # cycle instead of degrading it. The default keeps the primary feed the
    # source of truth while still recording personalized status per scan.
    PERSONALIZED_FEED_REQUIRED = _env_bool(
        "PERSONALIZED_FEED_REQUIRED",
        False,
    )

    PERSONALIZED_FEED_DISCOVERY = _env_bool(
        "PERSONALIZED_FEED_DISCOVERY",
        False,
    )

    # Verified against the live site: sort=1 is "Newest projects first" and
    # sort=2 is "Relevant first" (radio input value / dropdown data-value).
    # The parameter name and newest-first value are environment-configurable
    # because the marketplace may rename them; they must be set in lockstep
    # with FREELANCERMAP_PRIMARY_SEARCH_URL.
    FEED_QUERY_SORT_PARAM = os.getenv(
        "FREELANCERMAP_PRIMARY_FEED_SORT_PARAM",
        "sort",
    ).strip()

    PRIMARY_FEED_NEWEST_SORT_VALUE = os.getenv(
        "FREELANCERMAP_PRIMARY_FEED_NEWEST_SORT_VALUE",
        "1",
    ).strip()

    SECONDARY_FEED_ALLOWED_SORT_VALUES = ("1", "2")

    ALLOW_INSECURE_HTTP = _env_bool(
        "ALLOW_INSECURE_HTTP",
        False,
    )

    ALLOW_CROSS_ORIGIN_URLS = _env_bool(
        "ALLOW_CROSS_ORIGIN_URLS",
        False,
    )

    REQUIRE_LOGIN = _env_bool(
        "FREELANCERMAP_REQUIRE_LOGIN",
        True,
    )

    LOGIN_EMAIL = os.getenv(
        "FREELANCERMAP_LOGIN_EMAIL",
        "",
    ).strip()

    LOGIN_PASSWORD = os.getenv(
        "FREELANCERMAP_LOGIN_PASSWORD",
        "",
    )

    INTERACTIVE_LOGIN_TIMEOUT_SECONDS = _env_int(
        "INTERACTIVE_LOGIN_TIMEOUT_SECONDS",
        600,
        minimum=30,
        maximum=3600,
    )

    LOGIN_RETRY_INTERVAL_SECONDS = _env_int(
        "LOGIN_RETRY_INTERVAL_SECONDS",
        300,
        minimum=60,
        maximum=24 * 3600,
    )

    # -------------------------------------------------------------------------
    # Browser and scan behavior
    # -------------------------------------------------------------------------

    HEADLESS = _env_bool(
        "HEADLESS",
        True,
    )

    # Opt-in flags for constrained Linux/container deployments. Not enabled by
    # default: --no-sandbox weakens Chrome's security model and
    # --disable-dev-shm-usage can hurt performance on normal desktops.
    CHROME_NO_SANDBOX = _env_bool(
        "CHROME_NO_SANDBOX",
        False,
    )

    CHROME_DISABLE_DEV_SHM_USAGE = _env_bool(
        "CHROME_DISABLE_DEV_SHM_USAGE",
        False,
    )

    PAGE_LOAD_TIMEOUT = _env_int(
        "PAGE_LOAD_TIMEOUT",
        45,
        minimum=5,
        maximum=300,
    )

    ELEMENT_WAIT_SECONDS = _env_int(
        "ELEMENT_WAIT_SECONDS",
        20,
        minimum=1,
        maximum=120,
    )

    SCRIPT_TIMEOUT_SECONDS = _env_int(
        "SCRIPT_TIMEOUT_SECONDS",
        30,
        minimum=1,
        maximum=300,
    )

    MAX_PAGES = _env_int(
        "MAX_PAGES",
        1,
        minimum=1,
        maximum=100,
    )

    MAX_SCROLLS_PER_PAGE = _env_int(
        "MAX_SCROLLS_PER_PAGE",
        6,
        minimum=0,
        maximum=100,
    )

    MAX_LOAD_MORE_CLICKS = _env_int(
        "MAX_LOAD_MORE_CLICKS",
        3,
        minimum=0,
        maximum=50,
    )

    LISTING_STABLE_ROUNDS = _env_int(
        "LISTING_STABLE_ROUNDS",
        2,
        minimum=1,
        maximum=10,
    )

    # Poll interval between listing-stability measurements. Each round
    # compares the project-route count, document height, visible loading
    # indicators, and load-more availability; LISTING_STABLE_ROUNDS
    # consecutive identical rounds are required before a listing is parsed.
    LISTING_STABILITY_POLL_SECONDS = _env_float(
        "LISTING_STABILITY_POLL_SECONDS",
        0.5,
        minimum=0.1,
        maximum=60.0,
    )

    SCROLL_PAUSE_SECONDS = _env_float(
        "SCROLL_PAUSE_SECONDS",
        2.0,
        minimum=0.1,
        maximum=60.0,
    )

    REQUEST_DELAY_MIN_SECONDS = _env_float(
        "REQUEST_DELAY_MIN_SECONDS",
        4.0,
        minimum=0.0,
        maximum=300.0,
    )

    REQUEST_DELAY_MAX_SECONDS = _env_float(
        "REQUEST_DELAY_MAX_SECONDS",
        8.0,
        minimum=0.0,
        maximum=300.0,
    )

    MAX_PROJECTS_PER_CYCLE = _env_int(
        "MAX_PROJECTS_PER_CYCLE",
        500,
        minimum=1,
        maximum=50_000,
    )

    MAX_DETAIL_PAGES_PER_CYCLE = _env_int(
        "MAX_DETAIL_PAGES_PER_CYCLE",
        30,
        minimum=1,
        maximum=5_000,
    )

    DETAIL_REFRESH_HOURS = _env_int(
        "DETAIL_REFRESH_HOURS",
        24,
        minimum=1,
        maximum=24 * 365,
    )

    RECENTLY_SEEN_HOURS = _env_int(
        "RECENTLY_SEEN_HOURS",
        72,
        minimum=1,
        maximum=24 * 365,
    )

    REFRESH_STALE_DETAILS = _env_bool(
        "REFRESH_STALE_DETAILS",
        True,
    )

    DETAIL_MAX_ATTEMPTS = _env_int(
        "DETAIL_MAX_ATTEMPTS",
        5,
        minimum=1,
        maximum=100,
    )

    CHECK_INTERVAL_SECONDS = _env_int(
        "CHECK_INTERVAL_SECONDS",
        600,
        minimum=60,
        maximum=7 * 24 * 3600,
    )

    # Privacy-safe by default: raw page HTML is only retained when explicitly
    # enabled, so logged-in page content never ends up on disk by accident.
    STORE_RAW_HTML = _env_bool(
        "STORE_RAW_HTML",
        False,
    )

    AUTO_BASELINE_ON_FIRST_RUN = _env_bool(
        "AUTO_BASELINE_ON_FIRST_RUN",
        False,
    )

    ALLOW_EMPTY_RESULTS = _env_bool(
        "ALLOW_EMPTY_RESULTS",
        False,
    )

    MAX_EMAIL_PROJECTS_PER_MESSAGE = _env_int(
        "MAX_EMAIL_PROJECTS_PER_MESSAGE",
        25,
        minimum=1,
        maximum=200,
    )

    TIMEZONE = os.getenv(
        "TIMEZONE",
        "Asia/Karachi",
    ).strip()

    # A zero-card result is retried inside the same cycle before being treated
    # as an actual failure. This protects against transient loading failures,
    # logged-out pages, blocked pages, and parser regressions.
    EMPTY_RESULT_RETRIES = _env_int(
        "EMPTY_RESULT_RETRIES",
        2,
        minimum=0,
        maximum=10,
    )

    EMPTY_RESULT_RETRY_SECONDS = _env_float(
        "EMPTY_RESULT_RETRY_SECONDS",
        15.0,
        minimum=1.0,
        maximum=600.0,
    )

    # Operational alerting can use this threshold to avoid sending an alert for
    # one isolated empty scan while still detecting repeated extraction failure.
    EMPTY_SCAN_ALERT_AFTER = _env_int(
        "EMPTY_SCAN_ALERT_AFTER",
        2,
        minimum=1,
        maximum=100,
    )

    # -------------------------------------------------------------------------
    # Diagnostics and health reporting
    # -------------------------------------------------------------------------

    DIAGNOSTICS_ENABLED = _env_bool(
        "DIAGNOSTICS_ENABLED",
        True,
    )

    # Privacy-safe by default: HTML and screenshot capture is off unless the
    # operator explicitly opts in, so authenticated page content and personal
    # data are never persisted without consent.
    DIAGNOSTIC_CAPTURE_HTML = _env_bool(
        "DIAGNOSTIC_CAPTURE_HTML",
        False,
    )

    DIAGNOSTIC_CAPTURE_SCREENSHOT = _env_bool(
        "DIAGNOSTIC_CAPTURE_SCREENSHOT",
        False,
    )

    DIAGNOSTIC_REDACT_SENSITIVE_DATA = _env_bool(
        "DIAGNOSTIC_REDACT_SENSITIVE_DATA",
        True,
    )

    DIAGNOSTIC_MAX_BUNDLES = _env_int(
        "DIAGNOSTIC_MAX_BUNDLES",
        25,
        minimum=1,
        maximum=500,
    )

    DIAGNOSTIC_MAX_HTML_BYTES = _env_int(
        "DIAGNOSTIC_MAX_HTML_BYTES",
        5_000_000,
        minimum=100_000,
        maximum=50_000_000,
    )

    HEARTBEAT_ENABLED = _env_bool(
        "HEARTBEAT_ENABLED",
        True,
    )

    HEARTBEAT_STALE_AFTER_SECONDS = _env_int(
        "HEARTBEAT_STALE_AFTER_SECONDS",
        max(900, CHECK_INTERVAL_SECONDS * 3),
        minimum=120,
        maximum=30 * 24 * 3600,
    )

    # -------------------------------------------------------------------------
    # SMTP and alert routing
    # -------------------------------------------------------------------------

    SMTP_HOST = os.getenv(
        "SMTP_HOST",
        "smtp.gmail.com",
    ).strip()

    SMTP_PORT = _env_int(
        "SMTP_PORT",
        587,
        minimum=1,
        maximum=65535,
    )

    SMTP_USERNAME = os.getenv(
        "SMTP_USERNAME",
        "",
    ).strip()

    SMTP_PASSWORD = os.getenv(
        "SMTP_PASSWORD",
        "",
    )

    SMTP_REQUIRE_AUTH = _env_bool(
        "SMTP_REQUIRE_AUTH",
        True,
    )

    SMTP_FROM_EMAIL = os.getenv(
        "SMTP_FROM_EMAIL",
        SMTP_USERNAME,
    ).strip()

    SMTP_FROM_NAME = os.getenv(
        "SMTP_FROM_NAME",
        "Freelancermap Monitor",
    ).strip()

    # Normal project-notification recipients.
    SMTP_TO_EMAILS = _env_csv(
        "SMTP_TO_EMAILS",
    )

    # Operational errors are routed separately. They are never silently sent
    # to normal project recipients because diagnostic data may be sensitive.
    SMTP_ERROR_TO_EMAILS = _env_csv(
        "SMTP_ERROR_TO_EMAILS",
    )

    SMTP_USE_SSL = _env_bool(
        "SMTP_USE_SSL",
        False,
    )

    SMTP_USE_STARTTLS = _env_bool(
        "SMTP_USE_STARTTLS",
        True,
    )

    SMTP_TIMEOUT_SECONDS = _env_int(
        "SMTP_TIMEOUT_SECONDS",
        45,
        minimum=5,
        maximum=300,
    )

    # Operational alerting defaults to enabled only when dedicated recipients
    # were configured.
    OPERATIONAL_ALERTS_ENABLED = _env_bool(
        "OPERATIONAL_ALERTS_ENABLED",
        bool(SMTP_ERROR_TO_EMAILS),
    )

    ERROR_EMAIL_COOLDOWN_MINUTES = _env_int(
        "ERROR_EMAIL_COOLDOWN_MINUTES",
        30,
        minimum=1,
        maximum=7 * 24 * 60,
    )

    ERROR_EMAIL_SUBJECT_PREFIX = os.getenv(
        "ERROR_EMAIL_SUBJECT_PREFIX",
        "[Freelancermap Monitor]",
    ).strip()

    ERROR_EMAIL_MAX_DETAIL_CHARS = _env_int(
        "ERROR_EMAIL_MAX_DETAIL_CHARS",
        12_000,
        minimum=500,
        maximum=100_000,
    )

    @classmethod
    def ensure_directories(cls) -> None:
        """
        Create directories owned by this application.

        Permission failures intentionally propagate immediately so a scheduled
        monitor does not start successfully while being unable to save its
        database, lock, diagnostics, profile, or heartbeat.
        """

        directories = {
            cls.DATA_DIR,
            cls.DATABASE_PATH.parent,
            cls.CHROME_PROFILE_DIR,
            cls.LOCK_PATH.parent,
            cls.DIAGNOSTICS_DIR,
            cls.HEARTBEAT_PATH.parent,
        }

        for directory in directories:
            directory.mkdir(
                parents=True,
                exist_ok=True,
            )

    @classmethod
    def validate_runtime(cls) -> list[str]:
        """
        Return all runtime configuration errors.

        Collecting all errors at once makes setup substantially easier than
        failing one environment variable at a time.
        """

        errors: list[str] = []

        if not cls.FEED_QUERY_SORT_PARAM or not re.fullmatch(
            r"[A-Za-z0-9_.\-\[\]]+", cls.FEED_QUERY_SORT_PARAM
        ):
            errors.append(
                "FREELANCERMAP_PRIMARY_FEED_SORT_PARAM must be a valid "
                "query-parameter name"
            )

        if not cls.PRIMARY_FEED_NEWEST_SORT_VALUE or re.search(
            r"[&=?#\s]", cls.PRIMARY_FEED_NEWEST_SORT_VALUE
        ):
            errors.append(
                "FREELANCERMAP_PRIMARY_FEED_NEWEST_SORT_VALUE must be a "
                "non-empty value without &, =, ?, #, or whitespace"
            )

        if (
            cls.PERSONALIZED_FEED_REQUIRED
            and not cls.PERSONALIZED_SEARCH_URL
        ):
            errors.append(
                "PERSONALIZED_FEED_REQUIRED=true requires a non-empty "
                "FREELANCERMAP_PERSONALIZED_SEARCH_URL"
            )

        urls = [
            ("FREELANCERMAP_BASE_URL", cls.BASE_URL),
            ("FREELANCERMAP_PROJECTS_URL", cls.PROJECTS_URL),
            ("FREELANCERMAP_LOGIN_URL", cls.LOGIN_URL),
            ("FREELANCERMAP_ACCOUNT_URL", cls.ACCOUNT_URL),
            ("FREELANCERMAP_PRIMARY_SEARCH_URL", cls.PRIMARY_SEARCH_URL),
        ]
        if cls.PERSONALIZED_SEARCH_URL:
            urls.append(("FREELANCERMAP_PERSONALIZED_SEARCH_URL", cls.PERSONALIZED_SEARCH_URL))

        for name, value in urls:
            parsed = urlparse(value)

            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                errors.append(
                    f"{name} must be an absolute HTTP(S) URL"
                )
                continue

            if parsed.username or parsed.password:
                errors.append(
                    f"{name} must not contain embedded credentials"
                )

            if parsed.scheme == "http" and not cls.ALLOW_INSECURE_HTTP:
                errors.append(
                    f"{name} uses insecure HTTP; use HTTPS or explicitly set "
                    "ALLOW_INSECURE_HTTP=true only for a trusted local test "
                    "environment"
                )

            if name == "FREELANCERMAP_PRIMARY_SEARCH_URL":
                sort_value = _query_param_value(parsed, cls.FEED_QUERY_SORT_PARAM)
                if sort_value != cls.PRIMARY_FEED_NEWEST_SORT_VALUE:
                    errors.append(
                        f"{name} must be sorted newest-first (sort="
                        f"{cls.PRIMARY_FEED_NEWEST_SORT_VALUE}). The monitor "
                        "refuses to silently treat an unsorted or differently "
                        "sorted feed as newest-first."
                    )
            elif name == "FREELANCERMAP_PERSONALIZED_SEARCH_URL":
                sort_value = _query_param_value(parsed, cls.FEED_QUERY_SORT_PARAM)
                if sort_value not in cls.SECONDARY_FEED_ALLOWED_SORT_VALUES:
                    errors.append(
                        f"{name} must include a supported sort parameter "
                        f"(sort={cls.SECONDARY_FEED_ALLOWED_SORT_VALUES[0]} "
                        "newest-first or "
                        f"sort={cls.SECONDARY_FEED_ALLOWED_SORT_VALUES[1]} "
                        "relevant-first)"
                    )

            if name in (
                "FREELANCERMAP_PRIMARY_SEARCH_URL",
                "FREELANCERMAP_PERSONALIZED_SEARCH_URL",
            ):
                if not _is_feed_listing_path(parsed.path):
                    errors.append(
                        f"{name} must point to a project listing/search route, "
                        "not a detail, login, account, or app route: "
                        f"{parsed.path}"
                    )

        # Prevent a modified environment file from sending login credentials or
        # authenticated browser traffic to an unrelated origin.
        if not cls.ALLOW_CROSS_ORIGIN_URLS:
            base_origin = _url_origin(cls.BASE_URL)

            for name, value in urls[1:]:
                if _url_origin(value) != base_origin:
                    errors.append(
                        f"{name} must use the same origin as "
                        "FREELANCERMAP_BASE_URL; set "
                        "ALLOW_CROSS_ORIGIN_URLS=true only when this is "
                        "intentional"
                    )

        if cls.REQUEST_DELAY_MAX_SECONDS < cls.REQUEST_DELAY_MIN_SECONDS:
            errors.append(
                "REQUEST_DELAY_MAX_SECONDS must be greater than or equal to "
                "REQUEST_DELAY_MIN_SECONDS"
            )

        try:
            ZoneInfo(cls.TIMEZONE)
        except ZoneInfoNotFoundError:
            errors.append(
                f"TIMEZONE is unknown: {cls.TIMEZONE}"
            )

        file_paths = {
            "DATABASE_PATH": cls.DATABASE_PATH,
            "LOCK_PATH": cls.LOCK_PATH,
            "HEARTBEAT_PATH": cls.HEARTBEAT_PATH,
        }

        if len(set(file_paths.values())) != len(file_paths):
            errors.append(
                "DATABASE_PATH, LOCK_PATH, and HEARTBEAT_PATH must refer to "
                "different files"
            )

        if cls.DATABASE_PATH.exists() and cls.DATABASE_PATH.is_dir():
            errors.append(
                "DATABASE_PATH points to a directory, not a file"
            )

        if cls.LOCK_PATH.exists() and cls.LOCK_PATH.is_dir():
            errors.append(
                "LOCK_PATH points to a directory, not a file"
            )

        if cls.HEARTBEAT_PATH.exists() and cls.HEARTBEAT_PATH.is_dir():
            errors.append(
                "HEARTBEAT_PATH points to a directory, not a file"
            )

        if _contains_header_injection(cls.ERROR_EMAIL_SUBJECT_PREFIX):
            errors.append(
                "ERROR_EMAIL_SUBJECT_PREFIX contains an invalid newline"
            )

        return errors

    @classmethod
    def validate_email(cls) -> list[str]:
        """
        Validate SMTP configuration for normal project notifications.
        """

        return cls._validate_smtp(
            recipients=cls.SMTP_TO_EMAILS,
            recipient_name="SMTP_TO_EMAILS",
        )

    @classmethod
    def validate_operational_email(cls) -> list[str]:
        """
        Validate SMTP configuration for operational-error alerts.

        Empty operational recipients are allowed only when operational alerts
        are disabled. Project recipients are deliberately not reused.
        """

        if not cls.OPERATIONAL_ALERTS_ENABLED:
            return []

        return cls._validate_smtp(
            recipients=cls.SMTP_ERROR_TO_EMAILS,
            recipient_name="SMTP_ERROR_TO_EMAILS",
        )

    @classmethod
    def _validate_smtp(
        cls,
        *,
        recipients: list[str],
        recipient_name: str,
    ) -> list[str]:
        errors: list[str] = []

        if not cls.SMTP_HOST:
            errors.append(
                "SMTP_HOST is missing"
            )
        elif _contains_header_injection(cls.SMTP_HOST):
            errors.append(
                "SMTP_HOST contains an invalid newline"
            )

        if cls.SMTP_REQUIRE_AUTH:
            if not cls.SMTP_USERNAME:
                errors.append(
                    "SMTP_USERNAME is missing"
                )

            if not cls.SMTP_PASSWORD:
                errors.append(
                    "SMTP_PASSWORD is missing"
                )

        if _contains_header_injection(cls.SMTP_USERNAME):
            errors.append(
                "SMTP_USERNAME contains an invalid newline"
            )

        if _contains_header_injection(cls.SMTP_PASSWORD):
            errors.append(
                "SMTP_PASSWORD contains an invalid newline"
            )

        if not _valid_email_address(cls.SMTP_FROM_EMAIL):
            errors.append(
                "SMTP_FROM_EMAIL is missing or invalid"
            )

        if not recipients:
            errors.append(
                f"{recipient_name} is missing"
            )
        else:
            invalid = [
                address
                for address in recipients
                if not _valid_email_address(address)
            ]

            if invalid:
                errors.append(
                    f"{recipient_name} contains invalid address(es): "
                    + ", ".join(invalid)
                )

        if cls.SMTP_USE_SSL and cls.SMTP_USE_STARTTLS:
            errors.append(
                "Enable either SMTP_USE_SSL or SMTP_USE_STARTTLS, not both"
            )

        if not cls.SMTP_USE_SSL and not cls.SMTP_USE_STARTTLS:
            errors.append(
                "SMTP transport encryption is disabled; enable SSL or STARTTLS"
            )

        if not cls.SMTP_FROM_NAME:
            errors.append(
                "SMTP_FROM_NAME is missing"
            )
        elif _contains_header_injection(cls.SMTP_FROM_NAME):
            errors.append(
                "SMTP_FROM_NAME contains an invalid newline"
            )

        return errors


# Preserve the original project's convenient behavior while keeping all path
# resolution deterministic.
Config.ensure_directories()