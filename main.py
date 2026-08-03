from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from sqlite3 import Row
from typing import Any, Sequence

import database
from browser import BrowserSession
from config import Config
from emailer import send_test_email
from monitor import CycleResult, run_cycle, validate_detail
from parser import ProjectDiscovery, parse_project_detail, parse_project_links
from utils import ensure_query_param, utc_now_iso


LOGGER = logging.getLogger(__name__)
_PROCESS_STARTED_AT = utc_now_iso()


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc

    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")

    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Freelancermap project monitor with SQLite storage "
            "and SMTP notifications"
        ),
        allow_abbrev=False,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    action = parser.add_mutually_exclusive_group()

    action.add_argument(
        "--run-once",
        action="store_true",
        help="Run one scan cycle and exit",
    )

    action.add_argument(
        "--initialize-baseline",
        action="store_true",
        help=(
            "Store all currently visible projects as baseline "
            "without emailing them"
        ),
    )

    action.add_argument(
        "--interactive-login",
        action="store_true",
        help=(
            "Open visible Chrome for manual login and save "
            "the authenticated browser profile"
        ),
    )

    action.add_argument(
        "--test-login",
        action="store_true",
        help="Check whether the saved browser profile is authenticated",
    )

    action.add_argument(
        "--login-with-credentials",
        action="store_true",
        help=(
            "Attempt login using credentials from .env; "
            "CAPTCHA and MFA still require manual login"
        ),
    )

    action.add_argument(
        "--test-browser",
        action="store_true",
        help=(
            "Parse the live listing and one live project detail page "
            "without writing projects to SQLite"
        ),
    )

    action.add_argument(
        "--send-test-email",
        action="store_true",
        help="Send an SMTP test email",
    )

    action.add_argument(
        "--show-search-configuration",
        action="store_true",
        help="Print search feeds, authentication settings, and profile info",
    )

    action.add_argument(
        "--health-check",
        action="store_true",
        help="Check runtime configuration, SMTP, and SQLite health",
    )

    action.add_argument(
        "--db-status",
        action="store_true",
        help="Show database counts and SQLite integrity",
    )

    action.add_argument(
        "--list-projects",
        action="store_true",
        help="Show recently stored projects",
    )

    action.add_argument(
        "--recent-scans",
        action="store_true",
        help="Show recent scan-cycle records",
    )

    action.add_argument(
        "--export-csv",
        type=Path,
        metavar="PATH",
        help="Export all stored projects to CSV",
    )

    action.add_argument(
        "--backup-db",
        type=Path,
        metavar="PATH",
        help="Create a consistent SQLite backup",
    )

    action.add_argument(
        "--retry-failed-details",
        action="store_true",
        help="Reset failed detail pages to pending",
    )

    action.add_argument(
        "--reset-baseline",
        action="store_true",
        help=(
            "Void the current baseline so the next cycle re-baselines; "
            "requires --confirm-reset-baseline"
        ),
    )

    parser.add_argument(
        "--confirm-reset-baseline",
        action="store_true",
        help="Confirm a destructive baseline reset",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Scan and save projects, but do not send or mark "
            "project-alert emails"
        ),
    )

    parser.add_argument(
        "--visible",
        action="store_true",
        help="Use visible Chrome for browser-based commands",
    )

    parser.add_argument(
        "--limit",
        type=positive_int,
        default=20,
        help="Row display limit for listing commands",
    )

    parser.add_argument(
        "--log-level",
        choices=(
            "DEBUG",
            "INFO",
            "WARNING",
            "ERROR",
            "CRITICAL",
        ),
        default="INFO",
        help="Console and file logging level",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    configure_logging(args.log_level)

    try:
        ensure_directories()
        validate_runtime()

        if args.interactive_login:
            return interactive_login()

        if args.login_with_credentials:
            return credential_login()

        if args.test_login:
            return test_login(
                visible=args.visible,
            )

        if args.test_browser:
            return test_browser(
                visible=args.visible,
            )

        if args.send_test_email:
            validate_email()

            message_id = send_test_email()

            print(
                "SMTP test email sent successfully. "
                f"Message-ID: {message_id}"
            )

            return 0

        if args.show_search_configuration:
            return show_search_configuration()

        database.initialize_database()

        if args.reset_baseline:
            if not args.confirm_reset_baseline:
                print(
                    "Refusing to reset the baseline without confirmation. "
                    "Re-run with --reset-baseline --confirm-reset-baseline."
                )
                return 1
            changed = database.reset_baseline()
            print(
                f"Baseline voided; {changed} project row(s) re-marked as "
                "baseline. The next cycle will re-baseline."
            )
            return 0

        if args.health_check:
            return health_check()

        if args.db_status:
            return print_database_status()

        if args.list_projects:
            return print_projects(
                args.limit,
            )

        if args.recent_scans:
            return print_recent_scans(
                args.limit,
            )

        if args.export_csv is not None:
            path = args.export_csv.expanduser().resolve(
                strict=False,
            )

            count = database.export_csv(
                path,
            )

            print(
                f"Exported {count} project(s) to {path}"
            )

            return 0

        if args.backup_db is not None:
            return backup_database(
                args.backup_db,
            )

        if args.retry_failed_details:
            count = database.reset_failed_details()

            print(
                f"Reset {count} failed detail page(s) to pending."
            )

            return 0

        if args.initialize_baseline:
            result = run_cycle(
                dry_run=True,
                force_baseline=True,
                headless=headless_override(args.visible),
            )

            write_heartbeat(
                "success",
                "baseline",
                result=result,
            )

            print(
                result_line(result)
            )

            return 0

        # Running main.py without an explicit action starts continuous mode.
        #
        # SMTP is validated before scanning unless this is a dry run. This
        # prevents an unattended monitor from scraping projects successfully
        # for hours while being unable to deliver notifications.
        if not args.dry_run:
            validate_email()

        if args.run_once:
            return run_once(
                dry_run=args.dry_run,
                visible=args.visible,
            )

        return run_continuously(
            dry_run=args.dry_run,
            visible=args.visible,
        )

    except KeyboardInterrupt:
        write_heartbeat(
            "stopped",
            "command",
        )

        print("\nMonitor stopped.")

        return 0

    except Exception as exc:
        LOGGER.exception(
            "Command failed"
        )

        write_heartbeat(
            "failure",
            "command",
            error=exc,
        )

        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )

        return 1


def interactive_login() -> int:
    timeout_seconds = int(
        getattr(
            Config,
            "INTERACTIVE_LOGIN_TIMEOUT_SECONDS",
            600,
        )
    )

    with BrowserSession(
        headless=False,
    ) as browser:
        success = browser.interactive_login(
            timeout_seconds=timeout_seconds,
        )

    if success:
        print(
            "Login completed and the authenticated "
            "browser profile was saved."
        )

        return 0

    print(
        "Login was not completed before timeout. "
        "Run the command again and complete all login, "
        "CAPTCHA, or MFA steps in the opened browser.",
        file=sys.stderr,
    )

    return 1


def credential_login() -> int:
    with BrowserSession(
        headless=False,
    ) as browser:
        success = browser.login_with_credentials()

    if success:
        print(
            "Login completed and the authenticated "
            "browser profile was saved."
        )

        return 0

    print(
        "Credential login failed. Run: "
        "python main.py --interactive-login",
        file=sys.stderr,
    )

    return 1


def test_login(
    *,
    visible: bool,
) -> int:
    with BrowserSession(
        headless=headless_override(visible),
    ) as browser:
        res = browser.verify_authenticated_session()
        success = res.authenticated

    if success:
        print(
            "Login test passed: the saved session "
            "is authenticated."
        )

        return 0

    print(
        f"Login test failed ({res.reason}). Run: "
        "python main.py --interactive-login",
        file=sys.stderr,
    )

    return 1


def test_browser(
    *,
    visible: bool,
) -> int:
    """
    Run the production parsers against a live project card and detail page.

    Counting occurrences of '/project/' is not a valid browser test because
    those strings may occur in JavaScript, hidden navigation, or login-page
    markup. This command verifies the actual listing parser, detail parser,
    canonical URL handling, and semantic detail validation.
    """

    with BrowserSession(
        headless=headless_override(visible),
    ) as browser:
        if Config.REQUIRE_LOGIN:
            auth = browser.verify_authenticated_session()
            if not auth.authenticated:
                raise RuntimeError(
                    "The configured project feed requires an "
                    "authenticated session. Run: "
                    "python main.py --interactive-login"
                )
            print(
                f"Authenticated session  : yes ({auth.reason})"
            )

        listing_url = primary_feed_url()

        listing_html = browser.load_listing_page(
            listing_url,
            expected_sort=str(
                getattr(Config, "PRIMARY_FEED_NEWEST_SORT_VALUE", "1")
            ),
        )

        projects = parse_project_links(
            listing_html,
            Config.BASE_URL,
        )

        if not projects:
            raise RuntimeError(
                "The projects page loaded, but the production "
                "parser found no project cards. The page may be "
                "empty, blocked, filtered, still loading, or "
                "structurally changed."
            )

        # Prefer a card containing the greatest amount of visible data.
        # This provides a stronger validation than arbitrarily choosing the
        # first route found in the page.
        card = max(
            projects,
            key=discovery_richness,
        )

        if not (
            card.title_hint.strip()
            or card.card_description.strip()
        ):
            raise RuntimeError(
                "Project URLs were found, but neither project-card "
                "titles nor descriptions were extracted. The card "
                "parser likely needs updating."
            )

        detail_html = browser.get_project_page(
            card.url,
        )

        detail = parse_project_detail(
            detail_html,
            card.url,
            Config.BASE_URL,
        )

        validate_detail(
            detail,
        )

    print(
        "Browser and parser test passed."
    )

    print(
        f"Primary feed URL       : {listing_url}"
    )

    print(
        f"Projects parsed       : {len(projects)}"
    )

    print(
        "Card title            : "
        f"{card.title_hint or '(not present)'}"
    )

    print(
        "Card provider         : "
        f"{card.company_hint or '(not present)'}"
    )

    print(
        "Card description      : "
        f"{'yes' if card.card_description else 'no'}"
    )

    print(
        "Card location         : "
        f"{card.card_location or '(not present)'}"
    )

    print(
        "Card workplace        : "
        f"{card.card_workplace or '(not present)'}"
    )

    print(
        "Card contract type    : "
        f"{card.card_contract_type or '(not present)'}"
    )

    print(
        "Detail title          : "
        f"{detail.title}"
    )

    print(
        "Detail provider       : "
        f"{detail.company or '(not present)'}"
    )

    print(
        "Detail location       : "
        f"{detail.location or '(not present)'}"
    )

    print(
        "Detail workplace      : "
        f"{detail.workplace or '(not present)'}"
    )

    print(
        "Detail contract type  : "
        f"{detail.contract_type or '(not present)'}"
    )

    print(
        "Detail duration       : "
        f"{detail.duration or '(not present)'}"
    )

    print(
        "Detail start date     : "
        f"{detail.start_date or '(not present)'}"
    )

    print(
        "Detail workload       : "
        f"{detail.workload or '(not present)'}"
    )

    print(
        "Detail description    : "
        f"{'yes' if detail.description else 'no'}"
    )

    print(
        "Canonical URL         : "
        f"{detail.url}"
    )

    return 0


def primary_feed_url() -> str:
    """Build the primary feed URL with the configured newest-first sort key.

    The production discovery path always scans the primary feed with the
    configured sort parameter. The returned URL is used by both ``_discover``
    style scanning and ``--test-browser`` so the browser test proves the same
    URL the monitor fetches.
    """
    url = str(
        getattr(
            Config,
            "PRIMARY_SEARCH_URL",
            "",
        )
        or Config.PROJECTS_URL
    )
    param = str(getattr(Config, "FEED_QUERY_SORT_PARAM", "sort")).strip()
    value = str(getattr(Config, "PRIMARY_FEED_NEWEST_SORT_VALUE", "1")).strip()
    if param and value:
        url = ensure_query_param(url, param, value)
    return url


def run_once(
    *,
    dry_run: bool,
    visible: bool,
) -> int:
    write_heartbeat(
        "running",
        "once",
    )

    try:
        result = run_cycle(
            dry_run=dry_run,
            headless=headless_override(visible),
        )

    except Exception as exc:
        write_heartbeat(
            "failure",
            "once",
            error=exc,
        )

        raise

    write_heartbeat(
        "success",
        "once",
        result=result,
    )

    print(
        result_line(result)
    )

    return 0


def run_continuously(
    *,
    dry_run: bool,
    visible: bool,
) -> int:
    interval_seconds = int(
        Config.CHECK_INTERVAL_SECONDS
    )

    consecutive_failures = 0

    mode = (
        "continuous-dry-run"
        if dry_run
        else "continuous"
    )

    print(
        "Continuous monitor started. "
        f"Interval: {interval_seconds} seconds. "
        "Press Ctrl+C to stop."
    )

    while True:
        write_heartbeat(
            "running",
            mode,
            extra={
                "consecutive_failures": consecutive_failures,
            },
        )

        try:
            result = run_cycle(
                dry_run=dry_run,
                headless=headless_override(visible),
            )

            consecutive_failures = 0

            print(
                result_line(result),
                flush=True,
            )

            write_heartbeat(
                "success",
                mode,
                result=result,
                extra={
                    "consecutive_failures": 0,
                },
            )

        except Exception as exc:
            consecutive_failures += 1

            LOGGER.exception(
                "Scan cycle failed; retrying after the configured "
                "interval (consecutive failures: %d)",
                consecutive_failures,
            )

            write_heartbeat(
                "failure",
                mode,
                error=exc,
                extra={
                    "consecutive_failures": consecutive_failures,
                },
            )

        time.sleep(
            interval_seconds,
        )


def show_search_configuration() -> int:
    primary = getattr(Config, "PRIMARY_SEARCH_URL", Config.PROJECTS_URL)
    personalized = getattr(Config, "PERSONALIZED_SEARCH_URL", "")
    enable_sec = getattr(Config, "ENABLE_PERSONALIZED_FEED", False)
    sec_disc = getattr(Config, "PERSONALIZED_FEED_DISCOVERY", False)
    req_login = getattr(Config, "REQUIRE_LOGIN", True)
    prof_dir = getattr(Config, "CHROME_PROFILE_DIR", "")
    headless = getattr(Config, "HEADLESS", True)

    print("=== FREELANCERMAP MONITOR SEARCH CONFIGURATION ===")
    print(f"Primary Search URL       : {primary}")
    print(f"Personalized Feed        : {'ENABLED' if enable_sec else 'DISABLED'}")
    if enable_sec:
        print(f"  Personalized URL       : {personalized or '(not set)'}")
        print(f"  Secondary Discovery    : {'ENABLED' if sec_disc else 'DISABLED (Enrichment Only)'}")
    print(f"Require Authentication   : {'YES' if req_login else 'NO'}")
    print(f"Chrome Profile Directory : {prof_dir}")
    print(f"Headless Mode            : {'YES' if headless else 'NO'}")
    return 0


def health_check() -> int:
    runtime_errors = Config.validate_runtime()
    email_errors = Config.validate_email()
    database_health = database.database_health()

    operational_email_errors: list[str] = []

    operational_validator = getattr(
        Config,
        "validate_operational_email",
        None,
    )

    if callable(operational_validator):
        operational_email_errors = list(
            operational_validator()
        )

    print(
        "Runtime configuration : "
        f"{'OK' if not runtime_errors else 'FAILED'}"
    )

    for error in runtime_errors:
        print(
            f"  - {error}"
        )

    print(
        "Project SMTP          : "
        f"{'OK' if not email_errors else 'FAILED'}"
    )

    for error in email_errors:
        print(
            f"  - {error}"
        )

    if bool(
        getattr(
            Config,
            "OPERATIONAL_ALERTS_ENABLED",
            False,
        )
    ):
        print(
            "Operational SMTP      : "
            f"{'OK' if not operational_email_errors else 'FAILED'}"
        )

        for error in operational_email_errors:
            print(
                f"  - {error}"
            )

    print(
        "SQLite integrity      : "
        f"{'OK' if database_health['ok'] else 'FAILED'}"
    )

    print(
        "  schema_version      : "
        f"{database_health['schema_version']}"
    )

    print(
        "  integrity           : "
        f"{', '.join(database_health['integrity'])}"
    )

    print(
        "  foreign_key_errors  : "
        f"{database_health['foreign_key_errors']}"
    )

    failed = bool(
        runtime_errors
        or email_errors
        or operational_email_errors
        or not database_health["ok"]
    )

    return 1 if failed else 0


def print_database_status() -> int:
    health = database.database_health()

    for key, value in database.status_summary().items():
        print(
            f"{key:20}: {value}"
        )

    print(
        f"{'schema_version':20}: "
        f"{health['schema_version']}"
    )

    print(
        f"{'integrity':20}: "
        f"{', '.join(health['integrity'])}"
    )

    print(
        f"{'foreign_key_errors':20}: "
        f"{health['foreign_key_errors']}"
    )

    return 0 if health["ok"] else 1


def print_projects(
    limit: int,
) -> int:
    rows = database.list_projects(
        limit,
    )

    if not rows:
        print(
            "No projects stored."
        )

        return 0

    for row in rows:
        title = (
            row_get(row, "title")
            or row_get(row, "title_hint")
            or "(title pending)"
        )

        company = (
            row_get(row, "company")
            or row_get(row, "company_hint")
            or "(unknown provider)"
        )

        location = (
            row_get(row, "location")
            or "(unknown location)"
        )

        print(
            f"#{row['id']} | {title} | "
            f"{company} | {location}"
        )

        print(
            "   "
            f"contract="
            f"{row_get(row, 'contract_type') or '(unknown)'} | "
            f"workload="
            f"{row_get(row, 'workload') or '(unknown)'} | "
            f"detail="
            f"{row_get(row, 'detail_fetch_status')} | "
            f"email="
            f"{row_get(row, 'email_status')}"
        )

        print(
            f"   {row_get(row, 'url')}"
        )

    return 0


def print_recent_scans(
    limit: int,
) -> int:
    rows = database.recent_scans(
        limit,
    )

    if not rows:
        print(
            "No scan records stored."
        )

        return 0

    for row in rows:
        print(
            f"#{row['id']} | "
            f"status={row['status']} | "
            f"started={row['started_at']} | "
            f"finished={row['finished_at'] or '(running)'}"
        )

        print(
            "   "
            f"discovered={row['discovered_count']} | "
            f"new={row['new_count']} | "
            f"details_saved={row['detail_success_count']} | "
            f"detail_failures={row['detail_failure_count']} | "
            f"emailed={row['emailed_count']}"
        )

        print(
            "   "
            f"primary_feed={row_get(row, 'primary_feed_status', '')} | "
            f"personalized_feed={row_get(row, 'personalized_feed_status', '')} | "
            f"degraded={row_get(row, 'degraded', 0)}"
        )

        if row["error"]:
            print(
                "   error="
                f"{str(row['error'])[:500]}"
            )

    return 0


def backup_database(
    path: Path,
) -> int:
    destination = path.expanduser().resolve(
        strict=False,
    )

    source = Path(
        database.DATABASE_PATH
    ).expanduser().resolve(
        strict=False,
    )

    if destination == source:
        raise RuntimeError(
            "Backup destination must differ from "
            "the active database file."
        )

    database.backup_database(
        destination,
    )

    print(
        f"SQLite backup created: {destination}"
    )

    return 0


def result_line(
    result: CycleResult,
) -> str:
    mode = (
        "baseline"
        if result.baseline
        else "normal"
    )

    return (
        f"Cycle complete ({mode}): "
        f"discovered={result.discovered}, "
        f"new={result.new}, "
        f"details_saved={result.detail_success}, "
        f"detail_failures={result.detail_failure}, "
        f"emailed={result.emailed}, "
        f"primary_feed={result.primary_feed_status}, "
        f"personalized_feed={result.personalized_feed_status}, "
        f"degraded={result.degraded}"
    )


def discovery_richness(
    project: ProjectDiscovery,
) -> tuple[int, int]:
    values = (
        project.title_hint,
        project.company_hint,
        project.card_description,
        project.card_location,
        project.card_workplace,
        project.card_contract_type,
        project.card_duration,
        project.card_start_date,
        project.card_workload,
        project.card_rate,
    )

    populated_fields = sum(
        bool(value)
        for value in values
    )

    description_length = len(
        project.card_description or ""
    )

    return (
        populated_fields,
        description_length,
    )


def validate_runtime() -> None:
    errors = Config.validate_runtime()

    if errors:
        raise RuntimeError(
            "Runtime configuration error: "
            + "; ".join(errors)
        )


def validate_email() -> None:
    errors = Config.validate_email()

    if errors:
        raise RuntimeError(
            "SMTP configuration error: "
            + "; ".join(errors)
            + ". Use --dry-run to test scraping without email."
        )


def ensure_directories() -> None:
    method = getattr(
        Config,
        "ensure_directories",
        None,
    )

    if callable(method):
        method()
    else:
        Config.DATA_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )


def headless_override(
    visible: bool,
) -> bool | None:
    return False if visible else getattr(Config, "HEADLESS", True)


def configure_logging(
    level_name: str,
) -> None:
    Config.DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | "
        "%(name)s | %(message)s"
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(
        formatter
    )

    file_handler = RotatingFileHandler(
        Config.DATA_DIR / "freelancermap_monitor.log",
        maxBytes=5_000_000,
        backupCount=5,
        encoding="utf-8",
    )

    file_handler.setFormatter(
        formatter
    )

    logging.basicConfig(
        level=getattr(
            logging,
            level_name,
            logging.INFO,
        ),
        handlers=[
            console_handler,
            file_handler,
        ],
        force=True,
    )

    if level_name != "DEBUG":
        logging.getLogger(
            "selenium"
        ).setLevel(
            logging.WARNING
        )

        logging.getLogger(
            "urllib3"
        ).setLevel(
            logging.WARNING
        )


def write_heartbeat(
    status: str,
    mode: str,
    *,
    result: CycleResult | None = None,
    error: BaseException | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    if not bool(
        getattr(
            Config,
            "HEARTBEAT_ENABLED",
            False,
        )
    ):
        return

    raw_path = getattr(
        Config,
        "HEARTBEAT_PATH",
        None,
    )

    if raw_path is None:
        return

    payload: dict[str, Any] = {
        "service": "freelancermap-monitor",
        "pid": os.getpid(),
        "process_started_at": _PROCESS_STARTED_AT,
        "updated_at": utc_now_iso(),
        "status": status,
        "mode": mode,
    }

    if result is not None:
        payload["cycle"] = {
            "discovered": result.discovered,
            "new": result.new,
            "detail_success": result.detail_success,
            "detail_failure": result.detail_failure,
            "emailed": result.emailed,
            "baseline": result.baseline,
            "primary_feed_status": result.primary_feed_status,
            "personalized_feed_status": result.personalized_feed_status,
            "degraded": result.degraded,
        }
        if result.degraded_reason:
            payload["cycle"]["degraded_reason"] = result.degraded_reason

    if error is not None:
        payload["error"] = {
            "type": type(error).__name__,
            "message": str(error)[:2_000],
        }

    if extra:
        payload.update(
            extra
        )

    path = Path(
        raw_path
    )

    temporary_path = path.with_name(
        f".{path.name}.{os.getpid()}.tmp"
    )

    try:
        path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        temporary_path.write_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ),
            encoding="utf-8",
        )

        os.replace(
            temporary_path,
            path,
        )

    except Exception:
        LOGGER.exception(
            "Could not write heartbeat file: %s",
            path,
        )

        temporary_path.unlink(
            missing_ok=True,
        )


def row_get(
    row: Row,
    key: str,
    default: Any = "",
) -> Any:
    if key in row.keys():
        return row[key]

    return default


if __name__ == "__main__":
    raise SystemExit(
        main()
    )