from __future__ import annotations

import inspect
import json
import logging
import os
import re
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from sqlite3 import Row
from typing import Any, Iterable, Sequence
from urllib.parse import urljoin, urlparse, urlunparse

import database
from browser import BrowserSession
from config import Config
from emailer import send_projects_email
from pagedetect import has_error_title
from parser import ProjectDetail, ProjectDiscovery, parse_project_detail, parse_project_links
from utils import canonicalize_url, ensure_query_param, exclusive_file_lock, polite_sleep, utc_now_iso


LOGGER = logging.getLogger(__name__)

_PROJECT_PATH_RE = re.compile(r"^/project/[^/?#]+/?$", re.IGNORECASE)
_FATAL_BROWSER_MARKERS = (
    "chrome not reachable",
    "disconnected: not connected to devtools",
    "invalid session id",
    "no such window",
    "session deleted because of page crash",
    "session not created",
    "tab crashed",
    "target window already closed",
    "web view not found",
)
_SENSITIVE_QUERY_RE = re.compile(
    r"(?i)([?&](?:access_token|auth|code|key|password|session|token)=)[^&#\s\"']+"
)
_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_INPUT_VALUE_RE = re.compile(
    r"(<input\b[^>]*?\bvalue\s*=\s*)([\"']).*?\2",
    re.IGNORECASE | re.DOTALL,
)


class DiscoveryError(RuntimeError):
    """The listing page could not be extracted safely."""


class DetailValidationError(ValueError):
    """A project detail page did not contain trustworthy project data."""


class BrowserSessionLostError(RuntimeError):
    """Chrome/Selenium became unusable and the whole cycle must stop."""


@dataclass(slots=True)
class CycleResult:
    discovered: int = 0
    new: int = 0
    detail_success: int = 0
    detail_failure: int = 0
    emailed: int = 0
    baseline: bool = False
    primary_feed_status: str = "not_run"
    personalized_feed_status: str = "not_configured"
    degraded: bool = False
    degraded_reason: str = ""
    primary_count: int = 0
    personalized_count: int = 0
    personalized_only_count: int = 0
    ignored_personalized_only_count: int = 0


@dataclass(slots=True)
class DiscoveryOutcome:
    """Result of a discovery pass, including per-feed health."""

    projects: list[ProjectDiscovery]
    primary_feed_status: str = "ok"
    personalized_feed_status: str = "not_configured"
    primary_count: int = 0
    personalized_count: int = 0
    personalized_only_count: int = 0
    ignored_personalized_only_count: int = 0
    degraded: bool = False
    degraded_reason: str = ""


class PersonalizedFeedError(RuntimeError):
    """The personalized feed failed while PERSONALIZED_FEED_REQUIRED=true."""

    def __init__(self, message: str, outcome: DiscoveryOutcome) -> None:
        super().__init__(message)
        self.outcome = outcome


def run_cycle(
    *,
    dry_run: bool = False,
    force_baseline: bool = False,
    headless: bool | None = None,
) -> CycleResult:
    """Run one complete, single-process monitoring cycle.

    Reliability properties:

    * a non-blocking process lock prevents overlapping local runs;
    * first-run baseline state is persisted before any inserts, so an
      interrupted baseline resumes safely instead of emailing old projects;
    * empty or badly under-parsed listing pages are retried and rejected;
    * all discovered cards are stored or the cycle fails -- there is no silent
      slicing at ``MAX_PROJECTS_PER_CYCLE``;
    * one broken detail page does not stop other projects, but a dead Selenium
      session aborts immediately instead of marking every project as failed;
    * SMTP acceptance is journaled locally before rows are marked sent, reducing
      duplicate alerts if the process crashes between SMTP and SQLite commits;
    * scan history is finalized on both success and failure.
    """

    lock_path = Path(
        getattr(
            Config,
            "LOCK_PATH",
            Path(Config.DATA_DIR) / "freelancermap_monitor.lock",
        )
    )
    with exclusive_file_lock(lock_path):
        return _run_cycle(
            dry_run=dry_run,
            force_baseline=force_baseline,
            headless=headless,
        )


def _run_cycle(
    *,
    dry_run: bool,
    force_baseline: bool,
    headless: bool | None,
) -> CycleResult:
    _validate_runtime_configuration()
    database.initialize_database()
    _reconcile_accepted_email_receipts()

    baseline_initializing = _setting_bool("baseline_initializing")
    baseline_initialized = _baseline_initialized()
    baseline_needed = bool(
        force_baseline
        or baseline_initializing
        or (
            bool(getattr(Config, "AUTO_BASELINE_ON_FIRST_RUN", True))
            and not baseline_initialized
        )
    )

    # The first-run refusal happens BEFORE a scan row is created and before
    # any baseline state is persisted or browser is started. A refused run
    # must leave zero running scans, zero project mutations, zero baseline
    # state changes, zero email attempts, and no browser startup.
    if (
        not baseline_needed
        and not baseline_initialized
        and not bool(getattr(Config, "AUTO_BASELINE_ON_FIRST_RUN", False))
    ):
        raise RuntimeError(
            "Baseline is not initialized. Run: "
            "python main.py --initialize-baseline --visible"
        )

    scan_at = utc_now_iso()
    scan_id = database.create_scan()
    result = CycleResult()
    result.baseline = baseline_needed

    # Persist this state before inserting any cards. If Python, Chrome, Windows,
    # or the machine stops midway, the next run resumes baseline mode.
    if result.baseline:
        _set_setting("baseline_initializing", "true")
        _set_setting("baseline_started_at", scan_at)

    current_project_ids: list[int] = []
    browser: BrowserSession | None = None

    try:
        with BrowserSession(headless=headless) as browser:
            _require_authenticated_session(browser)
            try:
                outcome = _discover(browser, scan_at=scan_at)
            except PersonalizedFeedError as exc:
                result.primary_feed_status = exc.outcome.primary_feed_status
                result.personalized_feed_status = exc.outcome.personalized_feed_status
                result.degraded = exc.outcome.degraded
                result.degraded_reason = exc.outcome.degraded_reason
                result.primary_count = exc.outcome.primary_count
                result.personalized_count = exc.outcome.personalized_count
                result.personalized_only_count = exc.outcome.personalized_only_count
                result.ignored_personalized_only_count = exc.outcome.ignored_personalized_only_count
                raise
            except Exception:
                result.primary_feed_status = "failed"
                raise

            discoveries = outcome.projects
            result.primary_feed_status = outcome.primary_feed_status
            result.personalized_feed_status = outcome.personalized_feed_status
            result.degraded = outcome.degraded
            result.degraded_reason = outcome.degraded_reason
            result.primary_count = outcome.primary_count
            result.personalized_count = outcome.personalized_count
            result.personalized_only_count = outcome.personalized_only_count
            result.ignored_personalized_only_count = outcome.ignored_personalized_only_count
            result.discovered = len(discoveries)

            if not discoveries:
                _record_empty_scan()
                if result.baseline:
                    raise DiscoveryError(
                        "Baseline initialization was refused because no project "
                        "cards were discovered. A logged-out, blocked, empty, or "
                        "redesigned page must never become a successful baseline."
                    )
                if not bool(getattr(Config, "ALLOW_EMPTY_RESULTS", False)):
                    raise DiscoveryError(
                        "No project cards were discovered. The page may be logged "
                        "out, blocked, still loading, filtered, or structurally "
                        "changed. Run: python main.py --test-browser --visible"
                    )
            else:
                _reset_empty_scan_counter()

            maximum = int(getattr(Config, "MAX_PROJECTS_PER_CYCLE", 500))
            if len(discoveries) > maximum:
                raise DiscoveryError(
                    f"Discovered {len(discoveries)} projects, which exceeds "
                    f"MAX_PROJECTS_PER_CYCLE={maximum}. Increase the setting; "
                    "the monitor refuses to silently discard projects."
                )

            for discovery in discoveries:
                project_id, created = database.upsert_discovery(
                    discovery,
                    baseline=result.baseline,
                    seen_in_primary=getattr(discovery, "_seen_in_primary", True),
                    seen_in_personalized=getattr(discovery, "_seen_in_personalized", False),
                    primary_position=getattr(discovery, "_primary_position", None),
                    personalized_position=getattr(discovery, "_personalized_position", None),
                )
                current_project_ids.append(int(project_id))
                result.new += int(bool(created))

            detail_rows = _projects_needing_details()
            for index, row in enumerate(detail_rows):
                if index:
                    polite_sleep(
                        float(getattr(Config, "REQUEST_DELAY_MIN_SECONDS", 4.0)),
                        float(getattr(Config, "REQUEST_DELAY_MAX_SECONDS", 8.0)),
                    )

                try:
                    detail_html, detail = _fetch_and_parse_detail(
                        browser,
                        row,
                        scan_at=scan_at,
                    )
                    database.save_project_detail(
                        int(row["id"]),
                        detail,
                        detail_html,
                    )
                    result.detail_success += 1
                    LOGGER.info("Saved project detail: %s", detail.title)

                except BrowserSessionLostError:
                    # Do not increment the per-project failure counter when the
                    # browser itself died. The project page is not at fault.
                    raise

                except Exception as exc:
                    result.detail_failure += 1
                    safe_error = _safe_error(exc)
                    with suppress(Exception):
                        database.mark_detail_failure(int(row["id"]), safe_error)
                    LOGGER.exception(
                        "Detail fetch failed for %s",
                        _row_value(row, "url", "(unknown URL)"),
                    )

        if result.baseline:
            if not current_project_ids:
                raise DiscoveryError(
                    "Baseline initialization was refused because no current "
                    "project IDs were stored."
                )

            marked = _mark_baseline_projects(current_project_ids)
            LOGGER.info(
                "Baseline initialized with %d visible project(s); %d row(s) marked.",
                len(current_project_ids),
                marked,
            )
            _set_setting("baseline_initialized", "true")
            _set_setting("baseline_initializing", "false")
            _set_setting("baseline_completed_at", utc_now_iso())

        elif not dry_run:
            result.emailed = _send_one_pending_email_batch()

        else:
            pending = _pending_email_projects()
            if pending:
                LOGGER.info(
                    "Dry run: %d pending project(s) would be emailed; database "
                    "email state was not changed.",
                    len(pending),
                )

        _finish_scan_safely(
            scan_id,
            status="success",
            result=result,
        )
        return result

    except Exception as exc:
        if browser is not None:
            _capture_diagnostic(
                browser,
                category="cycle-failure",
                error=exc,
            )

        # A baseline attempt that failed before any project row was stored
        # (authentication, configuration, listing, or browser-startup
        # failure) must not leave baseline_initializing stuck on "true":
        # the next run would otherwise resume a baseline that never stored
        # anything. Nothing was mutated, so clear the markers and let the
        # next run re-evaluate from scratch.
        if result.baseline and not current_project_ids:
            try:
                _set_setting("baseline_initializing", "false")
                _set_setting("baseline_started_at", "")
            except Exception:
                LOGGER.exception(
                    "Could not clear baseline_initializing after an early "
                    "baseline failure."
                )

        _finish_scan_safely(
            scan_id,
            status="failed",
            result=result,
            error=_safe_error(exc),
        )
        raise


def _discover(
    browser: BrowserSession,
    *,
    scan_at: str,
) -> DiscoveryOutcome:
    """Load primary search feed and optional secondary feed, returning deduplicated cards.

    The primary feed is always required: a failed primary feed raises.  The
    personalized feed is optional; when it fails and
    ``PERSONALIZED_FEED_REQUIRED`` is false the cycle continues degraded,
    otherwise a :class:`PersonalizedFeedError` is raised.
    """

    primary_url = _safe_same_origin_url(
        ensure_query_param(
            str(getattr(Config, "PRIMARY_SEARCH_URL", Config.PROJECTS_URL)),
            str(getattr(Config, "FEED_QUERY_SORT_PARAM", "sort")),
            str(getattr(Config, "PRIMARY_FEED_NEWEST_SORT_VALUE", "1")),
        )
    )
    by_url: dict[str, ProjectDiscovery] = {}
    seen_listing_urls: set[str] = set()
    max_pages = int(getattr(Config, "MAX_PAGES", 1))
    outcome = DiscoveryOutcome(projects=[])
    personalized_required = bool(getattr(Config, "PERSONALIZED_FEED_REQUIRED", False))

    # 1. Scan Primary Feed (Newest First)
    listing_url = primary_url
    for page_number in range(1, max_pages + 1):
        if listing_url in seen_listing_urls:
            LOGGER.warning("Pagination loop detected at %s; stopping.", listing_url)
            break
        seen_listing_urls.add(listing_url)

        LOGGER.info("Scanning primary listing page %d: %s", page_number, listing_url)
        projects = _load_listing_with_retries(
            browser,
            listing_url,
            scan_at=scan_at,
            page_number=page_number,
            expected_sort=str(getattr(Config, "PRIMARY_FEED_NEWEST_SORT_VALUE", "1")),
        )

        for pos, project in enumerate(projects, 1):
            try:
                _validate_discovery(project)
            except Exception as exc:
                LOGGER.warning(
                    "Discarding an invalid project-card record: %s",
                    _safe_error(exc),
                )
                continue

            setattr(project, "_seen_in_primary", True)
            if not hasattr(project, "_primary_position"):
                setattr(project, "_primary_position", pos)

            key = canonicalize_url(project.url, Config.BASE_URL)
            existing = by_url.get(key)
            if existing is not None:
                merged = _richer_discovery(existing, project)
                setattr(merged, "_seen_in_primary", True)
                setattr(merged, "_primary_position", getattr(existing, "_primary_position", pos))
                by_url[key] = merged
            else:
                by_url[key] = project
            outcome.primary_count += 1

        if page_number >= max_pages:
            break

        next_url = browser.next_page_url()
        if not next_url:
            break
        next_url = _safe_same_origin_url(next_url)
        if next_url == listing_url or next_url in seen_listing_urls:
            LOGGER.warning("Pagination returned a repeated URL; stopping at %s", next_url)
            break

        listing_url = next_url
        polite_sleep(
            float(getattr(Config, "REQUEST_DELAY_MIN_SECONDS", 4.0)),
            float(getattr(Config, "REQUEST_DELAY_MAX_SECONDS", 8.0)),
        )

    # 2. Scan Optional Secondary Feed (Personalized / Relevant)
    enable_secondary = bool(getattr(Config, "ENABLE_PERSONALIZED_FEED", False))
    secondary_url = getattr(Config, "PERSONALIZED_SEARCH_URL", "").strip()
    allow_secondary_discovery = bool(getattr(Config, "PERSONALIZED_FEED_DISCOVERY", False))

    if not (enable_secondary and secondary_url):
        outcome.projects = list(by_url.values())
        return outcome

    secondary_safe = _safe_same_origin_url(secondary_url)
    LOGGER.info("Scanning optional secondary personalized feed: %s", secondary_safe)
    try:
        sec_projects = _load_listing_with_retries(
            browser,
            secondary_safe,
            scan_at=scan_at,
            page_number=1,
        )
        ignored_count = 0
        for pos, sec_project in enumerate(sec_projects, 1):
            try:
                _validate_discovery(sec_project)
            except Exception:
                continue
            setattr(sec_project, "_seen_in_personalized", True)
            setattr(sec_project, "_personalized_position", pos)
            key = canonicalize_url(sec_project.url, Config.BASE_URL)
            if key in by_url:
                existing = by_url[key]
                merged = _richer_discovery(existing, sec_project)
                setattr(merged, "_seen_in_primary", getattr(existing, "_seen_in_primary", True))
                setattr(merged, "_primary_position", getattr(existing, "_primary_position", None))
                setattr(merged, "_seen_in_personalized", True)
                setattr(merged, "_personalized_position", pos)
                by_url[key] = merged
            elif allow_secondary_discovery:
                setattr(sec_project, "_seen_in_primary", False)
                by_url[key] = sec_project
                outcome.personalized_only_count += 1
            else:
                ignored_count += 1
            outcome.personalized_count += 1
        if ignored_count > 0:
            LOGGER.info("Ignored %d secondary-only personalized projects (PERSONALIZED_FEED_DISCOVERY=false).", ignored_count)
        outcome.ignored_personalized_only_count = ignored_count
        outcome.personalized_feed_status = "ok"
    except Exception as exc:
        message = _safe_error(exc)
        LOGGER.warning("Secondary personalized feed scan encountered error: %s", message)
        outcome.personalized_feed_status = "failed"
        outcome.degraded = True
        outcome.degraded_reason = message
        if personalized_required:
            raise PersonalizedFeedError(
                "The configured personalized feed failed while "
                "PERSONALIZED_FEED_REQUIRED=true: " + message,
                outcome,
            ) from exc

    outcome.projects = list(by_url.values())
    return outcome


def _load_listing_with_retries(
    browser: BrowserSession,
    url: str,
    *,
    scan_at: str,
    page_number: int,
    expected_sort: str | None = None,
) -> list[ProjectDiscovery]:
    retries = max(0, int(getattr(Config, "EMPTY_RESULT_RETRIES", 2)))
    retry_delay = max(0.0, float(getattr(Config, "EMPTY_RESULT_RETRY_SECONDS", 15.0)))
    attempts = retries + 1
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            html = browser.load_listing_page(
                url,
                expected_sort=expected_sort,
            )
            projects = _parse_listing(html, scan_at)
            dom_count = _browser_project_route_count(browser)
            _validate_parser_coverage(
                parsed_count=len(projects),
                dom_route_count=dom_count,
                page_number=page_number,
            )

            if projects:
                if attempt > 1:
                    LOGGER.info(
                        "Listing extraction recovered on attempt %d/%d.",
                        attempt,
                        attempts,
                    )
                return projects

            last_error = DiscoveryError(
                f"Listing page {page_number} produced zero parsed project cards."
            )

        except BrowserSessionLostError:
            raise
        except Exception as exc:
            if _is_fatal_browser_error(exc):
                raise BrowserSessionLostError(_safe_error(exc)) from exc
            last_error = exc

        if attempt < attempts:
            LOGGER.warning(
                "Listing extraction attempt %d/%d failed for %s: %s. Retrying.",
                attempt,
                attempts,
                url,
                _safe_error(last_error),
            )
            time.sleep(retry_delay)

    _capture_diagnostic(
        browser,
        category=f"listing-page-{page_number}-failure",
        error=last_error,
    )
    raise DiscoveryError(
        f"Could not extract project cards from listing page {page_number} "
        f"after {attempts} attempt(s): {_safe_error(last_error)}"
    ) from last_error


def _parse_listing(html: str, scan_at: str) -> list[ProjectDiscovery]:
    parameters = inspect.signature(parse_project_links).parameters
    if "scan_at" in parameters:
        return list(parse_project_links(html, Config.BASE_URL, scan_at=scan_at))
    return list(parse_project_links(html, Config.BASE_URL))


def _fetch_and_parse_detail(
    browser: BrowserSession,
    row: Row,
    *,
    scan_at: str,
) -> tuple[str, ProjectDetail]:
    requested_url = _safe_project_url(str(row["url"]))
    retries = max(0, min(3, int(getattr(Config, "DETAIL_PAGE_RETRIES", 1))))
    attempts = retries + 1
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            html = _browser_get_project_page(browser, requested_url)
            detail = _parse_detail(html, requested_url, scan_at)
            validate_detail(detail, expected_url=requested_url)
            return html, detail

        except Exception as exc:
            if _is_fatal_browser_error(exc):
                _capture_diagnostic(
                    browser,
                    category="browser-session-lost",
                    error=exc,
                )
                raise BrowserSessionLostError(_safe_error(exc)) from exc

            last_error = exc
            if attempt < attempts:
                LOGGER.warning(
                    "Detail attempt %d/%d failed for %s: %s. Retrying.",
                    attempt,
                    attempts,
                    requested_url,
                    _safe_error(exc),
                )
                polite_sleep(
                    float(getattr(Config, "REQUEST_DELAY_MIN_SECONDS", 4.0)),
                    float(getattr(Config, "REQUEST_DELAY_MAX_SECONDS", 8.0)),
                )

    _capture_diagnostic(
        browser,
        category="detail-failure",
        error=last_error,
        requested_url=requested_url,
    )
    assert last_error is not None
    raise last_error


def _browser_get_project_page(browser: BrowserSession, url: str) -> str:
    method = getattr(browser, "get_project_page", None)
    if callable(method):
        return str(method(url))
    return str(browser.get(url))


def _parse_detail(html: str, url: str, scan_at: str) -> ProjectDetail:
    parameters = inspect.signature(parse_project_detail).parameters
    if "scan_at" in parameters:
        return parse_project_detail(
            html,
            url,
            Config.BASE_URL,
            scan_at=scan_at,
        )
    return parse_project_detail(html, url, Config.BASE_URL)


def validate_detail(
    detail: ProjectDetail,
    expected_url: str | None = None,
) -> None:
    """Reject login, block, unrelated, and incomplete pages before persistence."""

    title = str(detail.title or "").strip()
    if not title or has_error_title(title):
        raise DetailValidationError(
            "Project title was missing or looked like an access/login/error page."
        )

    parsed = urlparse(str(detail.url or ""))
    base = urlparse(Config.BASE_URL)
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise DetailValidationError("Canonical project URL is not an HTTP(S) URL.")
    if parsed.netloc.casefold() != base.netloc.casefold():
        raise DetailValidationError(
            "Canonical project URL points to an unexpected host."
        )
    if not _PROJECT_PATH_RE.fullmatch(parsed.path):
        raise DetailValidationError(
            "Canonical URL is not an exact Freelancermap project URL."
        )

    if expected_url:
        expected = urlparse(canonicalize_url(expected_url, Config.BASE_URL))
        if parsed.path.rstrip("/").casefold() != expected.path.rstrip("/").casefold():
            raise DetailValidationError(
                "The detail page canonical URL does not match the requested project."
            )

    meaningful_values = (
        detail.description,
        detail.company,
        detail.location,
        detail.workplace,
        detail.contract_type,
        detail.duration,
        detail.start_date,
        getattr(detail, "workload", ""),
        detail.rate,
        detail.contact_person,
        getattr(detail, "skills", []),
    )
    if not any(_has_content(value) for value in meaningful_values):
        raise DetailValidationError(
            "The page contained a title but none of the expected detail content "
            "such as a description or project metadata fields."
        )

    scan_at = str(getattr(detail, "scan_at", "") or "")
    posted_at = str(getattr(detail, "posted_at", "") or "")
    if scan_at:
        _require_aware_iso_timestamp(scan_at, "detail.scan_at")
    if posted_at:
        _require_aware_iso_timestamp(posted_at, "detail.posted_at")


def _validate_discovery(project: ProjectDiscovery) -> None:
    project.url = _safe_project_url(str(project.url))
    if not str(project.source_key or "").strip():
        raise DiscoveryError(f"Project card has no source key: {project.url}")

    scan_at = str(getattr(project, "scan_at", "") or "")
    posted_at = str(getattr(project, "posted_at", "") or "")
    if scan_at:
        _require_aware_iso_timestamp(scan_at, "discovery.scan_at")
    if posted_at:
        _require_aware_iso_timestamp(posted_at, "discovery.posted_at")


def _validate_parser_coverage(
    *,
    parsed_count: int,
    dom_route_count: int | None,
    page_number: int,
) -> None:
    if not dom_route_count:
        return

    if parsed_count == 0:
        raise DiscoveryError(
            f"The browser DOM contains {dom_route_count} unique project route(s), "
            "but the parser returned zero cards."
        )

    gap = dom_route_count - parsed_count
    ratio = parsed_count / dom_route_count
    minimum_ratio = float(getattr(Config, "MIN_PARSER_COVERAGE_RATIO", 0.70))
    minimum_gap = int(getattr(Config, "MIN_PARSER_COVERAGE_GAP", 3))

    if gap >= minimum_gap and ratio < minimum_ratio:
        raise DiscoveryError(
            f"Parser coverage is suspicious on listing page {page_number}: "
            f"parsed={parsed_count}, DOM project routes={dom_route_count}, "
            f"coverage={ratio:.0%}. Refusing a potentially incomplete scan."
        )


def _browser_project_route_count(browser: BrowserSession) -> int | None:
    method = getattr(browser, "_project_route_count", None)
    if callable(method):
        try:
            return int(method())
        except Exception:
            return None

    driver = getattr(browser, "driver", None)
    if driver is None or not hasattr(driver, "execute_script"):
        return None

    try:
        value = driver.execute_script(
            """
            const nodes = document.querySelectorAll(
              "a[href*='/project/'],[data-href*='/project/']," +
              "[data-url*='/project/'],[formaction*='/project/']"
            );
            const routes = new Set();
            for (const node of nodes) {
              for (const name of ['href','data-href','data-url','formaction']) {
                const value = node.getAttribute(name);
                if (value && value.includes('/project/')) {
                  const clean = value.split(/[?#]/)[0];
                  routes.add(clean.endsWith('/') ? clean.slice(0, -1) : clean);
                }
              }
            }
            return routes.size;
            """
        )
        return int(value or 0)
    except Exception:
        return None


def _projects_needing_details() -> list[Row]:
    limit = int(getattr(Config, "MAX_DETAIL_PAGES_PER_CYCLE", 30))
    method = database.projects_needing_details
    parameters = inspect.signature(method).parameters
    if "include_existing_stale" in parameters:
        return list(
            method(
                limit,
                include_existing_stale=bool(
                    getattr(Config, "REFRESH_STALE_DETAILS", True)
                ),
            )
        )
    return list(method(limit))


def _send_one_pending_email_batch() -> int:
    pending = _pending_email_projects()
    if not pending:
        return 0

    project_ids = [int(row["id"]) for row in pending]
    _ensure_email_receipt_directory()

    # Never retry inside this function. An SMTP disconnect after DATA can be
    # ambiguous: the server may have accepted the email even if the client saw
    # an exception. Keeping the rows pending lets the next scheduled cycle make
    # the policy decision, while deterministic Message-IDs reduce duplication.
    try:
        returned_message_id = send_projects_email(pending)
        message_id = str(returned_message_id or "")
    except Exception as exc:
        _mark_email_failure(project_ids, exc, message_id="")
        raise

    receipt_path = _write_accepted_email_receipt(
        project_ids=project_ids,
        message_id=message_id,
    )

    # Email-batch audit support is optional for compatibility with the minimal
    # database implementation. Audit failure must not cause a duplicate email.
    if message_id and hasattr(database, "start_email_batch"):
        try:
            database.start_email_batch(project_ids, message_id)
        except Exception:
            LOGGER.exception(
                "SMTP succeeded, but the optional email-batch audit row could "
                "not be created. Continuing to mark projects as sent."
            )

    try:
        _mark_projects_emailed(project_ids, message_id)
    except Exception:
        LOGGER.critical(
            "SMTP accepted Message-ID %s, but SQLite could not mark project IDs "
            "%s as sent. The accepted-email receipt was retained at %s and will "
            "be reconciled before the next send.",
            message_id or "(unknown)",
            project_ids,
            receipt_path,
            exc_info=True,
        )
        raise

    receipt_path.unlink(missing_ok=True)
    LOGGER.info(
        "SMTP accepted %d project notification(s); Message-ID=%s",
        len(project_ids),
        message_id or "(not returned)",
    )
    return len(project_ids)


def _pending_email_projects() -> list[Row]:
    method = database.pending_email_projects
    parameters = inspect.signature(method).parameters
    limit = int(getattr(Config, "MAX_EMAIL_PROJECTS_PER_MESSAGE", 25))
    if parameters:
        return list(method(limit))
    return list(method())


def _mark_projects_emailed(project_ids: Sequence[int], message_id: str) -> None:
    method = database.mark_projects_emailed
    parameters = inspect.signature(method).parameters
    if len(parameters) >= 2:
        method(project_ids, message_id)
    else:
        method(project_ids)


def _mark_email_failure(
    project_ids: Sequence[int],
    error: BaseException,
    *,
    message_id: str,
) -> None:
    method = getattr(database, "mark_email_failure", None)
    if not callable(method):
        return
    try:
        parameters = inspect.signature(method).parameters
        if len(parameters) >= 3:
            method(project_ids, _safe_error(error), message_id)
        else:
            method(project_ids, _safe_error(error))
    except Exception:
        LOGGER.exception("Could not persist the SMTP failure state.")


def _email_receipt_directory() -> Path:
    return Path(Config.DATA_DIR) / "smtp_receipts"


def _ensure_email_receipt_directory() -> Path:
    path = _email_receipt_directory()
    path.mkdir(parents=True, exist_ok=True)

    # Verify writability before SMTP submission, not after it.
    probe = path / f".write-test-{os.getpid()}"
    try:
        probe.write_text("ok", encoding="utf-8")
    finally:
        probe.unlink(missing_ok=True)
    return path


def _write_accepted_email_receipt(
    *,
    project_ids: Sequence[int],
    message_id: str,
) -> Path:
    directory = _ensure_email_receipt_directory()
    safe_token = re.sub(r"[^A-Za-z0-9_.-]+", "_", message_id.strip("<>") or utc_now_iso())
    safe_token = safe_token[:160]
    destination = directory / f"accepted-{safe_token}.json"
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    payload = {
        "version": 1,
        "accepted_at": utc_now_iso(),
        "message_id": message_id,
        "project_ids": [int(value) for value in project_ids],
    }
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, destination)
    return destination


def _reconcile_accepted_email_receipts() -> None:
    directory = _email_receipt_directory()
    if not directory.exists():
        return

    for path in sorted(directory.glob("accepted-*.json")):
        try:
            if path.stat().st_size > 100_000:
                raise ValueError("receipt is unexpectedly large")
            payload = json.loads(path.read_text(encoding="utf-8"))
            project_ids = [int(value) for value in payload.get("project_ids", [])]
            message_id = str(payload.get("message_id", ""))[:998]
            if not project_ids or len(project_ids) > 10_000:
                raise ValueError("receipt has invalid project IDs")
            _mark_projects_emailed(project_ids, message_id)
            path.unlink(missing_ok=True)
            LOGGER.warning(
                "Recovered an SMTP-accepted batch after an interrupted database "
                "update: projects=%s, Message-ID=%s",
                project_ids,
                message_id or "(unknown)",
            )
        except Exception:
            LOGGER.exception(
                "Could not reconcile accepted-email receipt %s. It was retained "
                "to prevent an unsafe automatic resend.",
                path,
            )
            # A receipt means SMTP previously accepted the message. Sending new
            # pending mail before reconciliation could duplicate that batch.
            raise RuntimeError(
                f"Unresolved accepted-email receipt: {path}. Fix the database "
                "or receipt before continuing."
            )


def _mark_baseline_projects(project_ids: Sequence[int]) -> int:
    method = getattr(database, "mark_projects_as_baseline", None)
    if callable(method):
        return int(method(project_ids) or 0)

    fallback = getattr(database, "mark_all_as_baseline", None)
    if callable(fallback):
        return int(fallback() or 0)

    # Minimal databases may set baseline during insert and expose no explicit
    # finalizer. In that case the setting still protects future runs.
    return len(project_ids)


def _baseline_initialized() -> bool:
    method = getattr(database, "baseline_initialized", None)
    if callable(method):
        return bool(method())
    return _setting_bool("baseline_initialized")


def _require_authenticated_session(browser: BrowserSession) -> None:
    if not bool(getattr(Config, "REQUIRE_LOGIN", True)):
        return
    res = browser.verify_authenticated_session()
    if not res.authenticated:
        raise RuntimeError(
            f"The configured Freelancermap feed requires an authenticated Chrome profile "
            f"({res.reason}). Run: python main.py --interactive-login"
        )


def _validate_runtime_configuration() -> None:
    validator = getattr(Config, "validate_runtime", None)
    if not callable(validator):
        return
    errors = list(validator())
    if errors:
        raise RuntimeError("Configuration error: " + "; ".join(errors))


def _finish_scan_safely(
    scan_id: int,
    *,
    status: str,
    result: CycleResult,
    error: str = "",
) -> None:
    try:
        database.finish_scan(
            scan_id,
            status=status,
            discovered_count=result.discovered,
            new_count=result.new,
            detail_success_count=result.detail_success,
            detail_failure_count=result.detail_failure,
            emailed_count=result.emailed,
            error=error,
            primary_feed_status=result.primary_feed_status,
            personalized_feed_status=result.personalized_feed_status,
            degraded=result.degraded,
            degraded_reason=result.degraded_reason,
            primary_count=result.primary_count,
            personalized_count=result.personalized_count,
            personalized_only_count=result.personalized_only_count,
            ignored_personalized_only_count=result.ignored_personalized_only_count,
        )
    except Exception:
        LOGGER.critical(
            "Could not finalize scan-history row %s with status=%s.",
            scan_id,
            status,
            exc_info=True,
        )


def _setting_bool(key: str) -> bool:
    getter = getattr(database, "get_setting", None)
    if not callable(getter):
        return False
    try:
        return str(getter(key, "false")).strip().casefold() == "true"
    except TypeError:
        return str(getter(key)).strip().casefold() == "true"


def _set_setting(key: str, value: str) -> None:
    setter = getattr(database, "set_setting", None)
    if callable(setter):
        setter(key, value)


def _record_empty_scan() -> None:
    getter = getattr(database, "get_setting", None)
    setter = getattr(database, "set_setting", None)
    if not callable(getter) or not callable(setter):
        return
    try:
        current = int(str(getter("consecutive_empty_scans", "0")) or "0")
    except (TypeError, ValueError):
        current = 0
    setter("consecutive_empty_scans", str(current + 1))
    setter("last_empty_scan_at", utc_now_iso())


def _reset_empty_scan_counter() -> None:
    _set_setting("consecutive_empty_scans", "0")


def _safe_same_origin_url(value: str) -> str:
    """Resolve one HTTP(S) URL and verify it stays on the configured origin.

    Unlike :func:`canonicalize_url` -- which strips query strings for project
    deduplication -- this helper preserves the query string and fragment so the
    configured search URLs keep their search query and newest-first sort keys.
    """
    clean = str(value or "").strip()
    if not clean:
        raise DiscoveryError("Empty URL rejected.")
    if "://" not in clean:
        absolute = urljoin(Config.BASE_URL, clean)
    else:
        absolute = clean
    parsed = urlparse(absolute)
    base = urlparse(Config.BASE_URL)
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise DiscoveryError(f"Unsupported URL scheme: {value}")
    if not parsed.netloc:
        raise DiscoveryError(f"URL has no host: {value}")
    if parsed.netloc.casefold() != base.netloc.casefold():
        raise DiscoveryError(f"Cross-origin URL rejected: {value}")
    return urlunparse(parsed)


def _safe_project_url(value: str) -> str:
    absolute = _safe_same_origin_url(value)
    parsed = urlparse(absolute)
    if not _PROJECT_PATH_RE.fullmatch(parsed.path):
        raise DiscoveryError(f"Not an exact Freelancermap project URL: {value}")
    return absolute


def _richer_discovery(
    left: ProjectDiscovery,
    right: ProjectDiscovery,
) -> ProjectDiscovery:
    return right if _discovery_score(right) > _discovery_score(left) else left


def _discovery_score(project: ProjectDiscovery) -> tuple[int, int, int]:
    fields = (
        project.title_hint,
        getattr(project, "company_hint", ""),
        getattr(project, "card_description", ""),
        getattr(project, "card_location", ""),
        getattr(project, "card_workplace", ""),
        getattr(project, "card_contract_type", ""),
        getattr(project, "card_duration", ""),
        getattr(project, "card_start_date", ""),
        getattr(project, "card_workload", ""),
        getattr(project, "card_rate", ""),
    )
    populated = sum(bool(str(value or "").strip()) for value in fields)
    description_length = len(str(getattr(project, "card_description", "") or ""))
    html_length = len(str(getattr(project, "card_html", "") or ""))
    return populated, description_length, html_length


def _has_content(value: Any) -> bool:
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return bool(str(value or "").strip())


def _require_aware_iso_timestamp(value: str, field_name: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} is not valid ISO 8601: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone offset.")


def _is_fatal_browser_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".casefold()
    return any(marker in text for marker in _FATAL_BROWSER_MARKERS)


def _safe_error(error: BaseException | None, limit: int = 2_000) -> str:
    if error is None:
        return "unknown error"
    value = f"{type(error).__name__}: {error}"
    value = _SENSITIVE_QUERY_RE.sub(r"\1[REDACTED]", value)
    value = _EMAIL_RE.sub("[REDACTED_EMAIL]", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit]


def _row_value(row: Row, key: str, default: Any = "") -> Any:
    try:
        return row[key] if key in row.keys() else default
    except AttributeError:
        if isinstance(row, dict):
            return row.get(key, default)
        return default


def _capture_diagnostic(
    browser: BrowserSession,
    *,
    category: str,
    error: BaseException | None,
    requested_url: str = "",
) -> None:
    if not bool(getattr(Config, "DIAGNOSTICS_ENABLED", False)):
        return

    driver = getattr(browser, "driver", None)
    if driver is None:
        return

    try:
        directory = Path(
            getattr(
                Config,
                "DIAGNOSTICS_DIR",
                Path(Config.DATA_DIR) / "diagnostics",
            )
        )
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        clean_category = re.sub(r"[^A-Za-z0-9_.-]+", "-", category).strip("-")[:80]
        stem = f"{stamp}-{clean_category or 'failure'}"

        current_url = str(getattr(driver, "current_url", "") or "")
        title = str(getattr(driver, "title", "") or "")
        metadata = {
            "captured_at": utc_now_iso(),
            "category": category,
            "requested_url": _redact_text(requested_url),
            "current_url": _redact_text(current_url),
            "page_title": _redact_text(title),
            "error": _safe_error(error),
        }
        (directory / f"{stem}.json").write_text(
            json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )

        if bool(getattr(Config, "DIAGNOSTIC_CAPTURE_HTML", False)):
            raw_html = str(getattr(driver, "page_source", "") or "")
            if bool(getattr(Config, "DIAGNOSTIC_REDACT_SENSITIVE_DATA", True)):
                raw_html = _redact_html(raw_html)
            max_bytes = int(getattr(Config, "DIAGNOSTIC_MAX_HTML_BYTES", 5_000_000))
            encoded = raw_html.encode("utf-8")[:max_bytes]
            (directory / f"{stem}.html").write_bytes(encoded)

        if bool(getattr(Config, "DIAGNOSTIC_CAPTURE_SCREENSHOT", False)) and hasattr(
            driver, "save_screenshot"
        ):
            driver.save_screenshot(str(directory / f"{stem}.png"))

        _prune_diagnostics(directory)

    except Exception:
        LOGGER.exception("Could not capture browser diagnostics.")


def _redact_html(value: str) -> str:
    value = _INPUT_VALUE_RE.sub(r"\1\2[REDACTED]\2", value)
    return _redact_text(value)


def _redact_text(value: str) -> str:
    value = _SENSITIVE_QUERY_RE.sub(r"\1[REDACTED]", value)
    return _EMAIL_RE.sub("[REDACTED_EMAIL]", value)


def _prune_diagnostics(directory: Path) -> None:
    maximum = max(1, int(getattr(Config, "DIAGNOSTIC_MAX_BUNDLES", 25)))
    metadata_files = sorted(
        directory.glob("*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for metadata in metadata_files[maximum:]:
        stem = metadata.stem
        for suffix in (".json", ".html", ".png"):
            (directory / f"{stem}{suffix}").unlink(missing_ok=True)