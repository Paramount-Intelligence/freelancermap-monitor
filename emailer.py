from __future__ import annotations

import hashlib
import html
import logging
import smtplib
import socket
import ssl
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from email.message import EmailMessage
from email.policy import SMTP as SMTP_POLICY
from email.utils import format_datetime, formataddr, parseaddr
from sqlite3 import Row
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from config import Config
from utils import normalize_space


LOGGER = logging.getLogger(__name__)
REQUIRED_RECIPIENT = "hafiz.muhammad.ibrahim.salman@gmail.com"
DEFAULT_SMTP_TIMEOUT_SECONDS = 45
DEFAULT_DESCRIPTION_LIMIT = 8_000


class EmailDeliveryError(RuntimeError):
    """SMTP could not accept the message for every intended recipient."""


def send_projects_email(projects: Sequence[Row | Mapping[str, Any]]) -> str:
    """Send one HTML/plain-text digest and return its Message-ID.

    This function deliberately performs no database writes. The caller must
    mark rows as sent only after this function returns successfully.
    """

    rows = list(projects)
    if not rows:
        raise ValueError("Cannot send an empty project digest.")

    _validate_smtp()
    recipients = _recipients()
    items = [_project_data(row) for row in rows]
    message_id = _digest_message_id(items)
    message = _build_digest(items, recipients, message_id)
    _send(message, recipients)
    return message_id


def send_project_digest(projects: Sequence[Row | Mapping[str, Any]]) -> str:
    """Compatibility alias for the minimal monitor specification."""

    return send_projects_email(projects)


def send_test_email() -> str:
    """Verify TLS, authentication, and SMTP submission."""

    _validate_smtp()
    recipients = _recipients()
    now = datetime.now(timezone.utc)
    message_id = _random_message_id("freelancermap-test")

    message = EmailMessage(policy=SMTP_POLICY)
    _set_headers(
        message,
        subject="Freelancermap Monitor SMTP Test",
        recipients=recipients,
        message_id=message_id,
        sent_at=now,
    )
    message.set_content(
        "Freelancermap Monitor SMTP test.\n\n"
        f"Submitted at: {_display_time(now.isoformat())}\n"
        f"Required recipient: {REQUIRED_RECIPIENT}\n",
        charset="utf-8",
    )
    message.add_alternative(
        f"""<!doctype html>
<html lang="en"><body style="margin:0;background:#f3f5f7;font-family:Arial,sans-serif;color:#17202a">
<table role="presentation" width="100%"><tr><td align="center" style="padding:28px 12px">
<table role="presentation" width="600" style="max-width:600px;background:#fff;border:1px solid #d9e1e6;border-radius:10px">
<tr><td style="padding:26px"><h1 style="margin:0 0 12px;color:#123f5d">SMTP test submitted</h1>
<p style="line-height:1.6">The monitor connected securely, authenticated, and submitted this message to the SMTP server.</p>
<p style="color:#60717d;font-size:13px">{html.escape(_display_time(now.isoformat()))}</p>
</td></tr></table></td></tr></table></body></html>""",
        subtype="html",
        charset="utf-8",
    )
    _send(message, recipients)
    return message_id


def _build_digest(
    projects: Sequence[dict[str, str]],
    recipients: Sequence[str],
    message_id: str,
) -> EmailMessage:
    now = datetime.now(timezone.utc)
    count = len(projects)
    noun = "project" if count == 1 else "projects"

    message = EmailMessage(policy=SMTP_POLICY)
    _set_headers(
        message,
        subject=f"{count} new Freelancermap {noun}",
        recipients=recipients,
        message_id=message_id,
        sent_at=now,
    )
    message["X-Freelancermap-Project-Count"] = str(count)
    message.set_content(_plain_text(projects, now), charset="utf-8")
    message.add_alternative(
        _html_email(projects, now),
        subtype="html",
        charset="utf-8",
    )
    return message


def _set_headers(
    message: EmailMessage,
    *,
    subject: str,
    recipients: Sequence[str],
    message_id: str,
    sent_at: datetime,
) -> None:
    sender_email = _sender()
    sender_name = _header(
        getattr(Config, "SMTP_FROM_NAME", "Freelancermap Monitor"),
        "SMTP_FROM_NAME",
    )

    message["Subject"] = _header(subject, "Subject")
    message["From"] = formataddr((sender_name, sender_email))
    message["To"] = REQUIRED_RECIPIENT

    extra_recipients = [
        address for address in recipients if address.casefold() != REQUIRED_RECIPIENT.casefold()
    ]
    if extra_recipients:
        message["Bcc"] = ", ".join(extra_recipients)

    reply_to = normalize_space(str(getattr(Config, "SMTP_REPLY_TO", "") or ""))
    if reply_to:
        if not _valid_email(reply_to):
            raise RuntimeError("SMTP_REPLY_TO is invalid.")
        message["Reply-To"] = reply_to

    message["Date"] = format_datetime(sent_at)
    message["Message-ID"] = message_id
    message["Auto-Submitted"] = "auto-generated"
    message["X-Auto-Response-Suppress"] = "All"
    message["X-Mailer"] = "Freelancermap Monitor"


def _send(message: EmailMessage, recipients: Sequence[str] | None = None) -> None:
    """Submit once over TLS; do not retry an ambiguous SMTP failure here."""

    recipients = list(recipients or _recipients())
    host = normalize_space(str(Config.SMTP_HOST))
    port = int(Config.SMTP_PORT)
    timeout = int(
        getattr(Config, "SMTP_TIMEOUT_SECONDS", DEFAULT_SMTP_TIMEOUT_SECONDS)
    )
    use_ssl = bool(Config.SMTP_USE_SSL)
    use_starttls = bool(Config.SMTP_USE_STARTTLS)
    require_auth = bool(getattr(Config, "SMTP_REQUIRE_AUTH", True))

    if use_ssl == use_starttls:
        raise RuntimeError(
            "Enable exactly one of SMTP_USE_SSL or SMTP_USE_STARTTLS."
        )
    if timeout <= 0:
        raise RuntimeError("SMTP_TIMEOUT_SECONDS must be greater than zero.")

    context = ssl.create_default_context()

    try:
        if use_ssl:
            with smtplib.SMTP_SSL(
                host,
                port,
                timeout=timeout,
                context=context,
            ) as server:
                _login(server, require_auth)
                refused = server.send_message(
                    message,
                    from_addr=_sender(),
                    to_addrs=recipients,
                )
        else:
            with smtplib.SMTP(host, port, timeout=timeout) as server:
                server.ehlo()
                if not server.has_extn("starttls"):
                    raise smtplib.SMTPNotSupportedError(
                        "SMTP server does not advertise STARTTLS."
                    )
                server.starttls(context=context)
                server.ehlo()
                _login(server, require_auth)
                refused = server.send_message(
                    message,
                    from_addr=_sender(),
                    to_addrs=recipients,
                )

        # send_message may return normally after accepting at least one address.
        # For this monitor, partial delivery is a failure because database rows
        # must not be marked sent unless every configured recipient was accepted.
        if refused:
            detail = ", ".join(
                f"{address} ({code}: {_smtp_text(response)})"
                for address, (code, response) in refused.items()
            )
            raise EmailDeliveryError(f"SMTP refused recipient(s): {detail}")

    except EmailDeliveryError:
        raise
    except smtplib.SMTPAuthenticationError as exc:
        raise EmailDeliveryError(
            "SMTP authentication failed. For Gmail, use a Google App Password, "
            "not the normal account password."
        ) from exc
    except smtplib.SMTPResponseException as exc:
        raise EmailDeliveryError(
            f"SMTP rejected the request ({exc.smtp_code}): "
            f"{_smtp_text(exc.smtp_error)}"
        ) from exc
    except (smtplib.SMTPException, TimeoutError, socket.timeout, OSError) as exc:
        raise EmailDeliveryError(
            f"SMTP delivery failed: {type(exc).__name__}: {exc}"
        ) from exc


def _login(server: smtplib.SMTP | smtplib.SMTP_SSL, required: bool) -> None:
    if not required:
        return
    username = str(getattr(Config, "SMTP_USERNAME", "") or "")
    password = str(getattr(Config, "SMTP_PASSWORD", "") or "")
    if not username or not password:
        raise RuntimeError("SMTP_USERNAME or SMTP_PASSWORD is missing.")
    server.login(username, password)


def _validate_smtp() -> None:
    errors = list(Config.validate_email())
    if errors:
        raise RuntimeError("SMTP configuration error: " + "; ".join(errors))
    if bool(Config.SMTP_USE_SSL) == bool(Config.SMTP_USE_STARTTLS):
        raise RuntimeError(
            "SMTP must use exactly one secure mode: SSL or STARTTLS."
        )


def _recipients() -> list[str]:
    values = [REQUIRED_RECIPIENT, *(getattr(Config, "SMTP_TO_EMAILS", []) or [])]
    output: list[str] = []
    seen: set[str] = set()

    for raw in values:
        address = normalize_space(str(raw or ""))
        key = address.casefold()
        if not address or key in seen:
            continue
        if not _valid_email(address):
            raise RuntimeError(f"Invalid SMTP recipient: {address!r}")
        output.append(address)
        seen.add(key)

    return output


def _sender() -> str:
    value = normalize_space(str(getattr(Config, "SMTP_FROM_EMAIL", "") or ""))
    if not _valid_email(value):
        raise RuntimeError("SMTP_FROM_EMAIL is missing or invalid.")
    return value


def _valid_email(value: str) -> bool:
    if not value or "\r" in value or "\n" in value:
        return False
    name, address = parseaddr(value)
    return not name and address == value and "@" in address


def _header(value: Any, name: str) -> str:
    raw = str(value or "")
    if "\r" in raw or "\n" in raw:
        raise RuntimeError(f"{name} contains an invalid newline.")
    clean = normalize_space(raw)
    if not clean:
        raise RuntimeError(f"{name} must not be empty.")
    return clean


def _project_data(row: Row | Mapping[str, Any]) -> dict[str, str]:
    scan_at = _value(row, "scan_at", "last_seen_at", "first_seen_at")
    posted_at = _value(row, "posted_at", "published_at") or scan_at
    title = _value(row, "title", "title_hint") or "Untitled project"
    url = _project_url(_value(row, "url", "canonical_url"))
    if not url:
        raise ValueError(f"Project {title!r} has no valid HTTP(S) URL.")

    description = _value(row, "description") or _combine_text(
        _value(row, "card_description"),
        _value(row, "detail_description"),
    )
    project_length = _value(row, "project_length") or _join_facts(
        ("Start", _value(row, "start_date")),
        ("Duration", _value(row, "duration")),
        ("Workload", _value(row, "workload")),
    )

    return {
        "id": _value(row, "id"),
        "scan_at": scan_at,
        "posted_at": posted_at or scan_at,
        "title": title,
        "description": description,
        "location": _value(row, "location", "card_location"),
        "project_length": project_length,
        "url": url,
        "budget": _value(row, "budget", "rate", "card_rate"),
        "engagement_type": _value(
            row,
            "engagement_type",
            "workload",
            "card_workload",
            "contract_type",
            "card_contract_type",
        ),
    }


def _plain_text(projects: Sequence[dict[str, str]], generated_at: datetime) -> str:
    lines = [
        f"{len(projects)} new Freelancermap project(s)",
        f"Generated: {_display_time(generated_at.isoformat())}",
        "",
    ]

    for number, project in enumerate(projects, 1):
        lines.extend(["=" * 68, f"{number}. {project['title']}", "=" * 68])
        for label, key in (
            ("Posted at", "posted_at"),
            ("Scan at", "scan_at"),
            ("Location", "location"),
            ("Project length", "project_length"),
            ("Budget", "budget"),
            ("Engagement type", "engagement_type"),
        ):
            value = _display_time(project[key]) if key.endswith("_at") else project[key]
            if value and value != "Not provided":
                lines.append(f"{label}: {value}")

        lines.extend(
            [
                "",
                "Description:",
                _email_description(project["description"]) or "Not provided",
                "",
                f"Exact project URL: {project['url']}",
                "",
            ]
        )

    return "\n".join(lines)


def _html_email(projects: Sequence[dict[str, str]], generated_at: datetime) -> str:
    count = len(projects)
    cards = "".join(_project_card(project, number) for number, project in enumerate(projects, 1))
    preheader = html.escape(
        f"{count} new Freelancermap project(s): "
        + ", ".join(project["title"] for project in projects[:3])
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;background:#f3f5f7;color:#17202a;font-family:Arial,Helvetica,sans-serif">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent">{preheader}</div>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="720" cellspacing="0" cellpadding="0" border="0" style="width:100%;max-width:720px">
<tr><td style="background:#123f5d;color:#fff;padding:26px 24px;border-radius:12px 12px 0 0">
<p style="margin:0 0 7px;font-size:12px;letter-spacing:.08em;text-transform:uppercase;opacity:.82">Freelancermap Monitor</p>
<h1 style="margin:0;font-size:25px">{count} new project{'s' if count != 1 else ''}</h1>
<p style="margin:9px 0 0;font-size:14px;opacity:.9">Generated {html.escape(_display_time(generated_at.isoformat()))}</p>
</td></tr><tr><td style="padding:18px;background:#e9eef2;border-radius:0 0 12px 12px">{cards}</td></tr>
</table></td></tr></table></body></html>"""


def _project_card(project: Mapping[str, str], number: int) -> str:
    url = html.escape(project["url"], quote=True)
    facts = "".join(
        _fact_row(label, _display_time(value) if key.endswith("_at") else value)
        for label, key, value in (
            ("Posted at", "posted_at", project["posted_at"]),
            ("Scan at", "scan_at", project["scan_at"]),
            ("Location", "location", project["location"]),
            ("Project length", "project_length", project["project_length"]),
            ("Budget", "budget", project["budget"]),
            ("Engagement type", "engagement_type", project["engagement_type"]),
        )
        if normalize_space(value)
    )
    description = html.escape(_email_description(project["description"])).replace("\n", "<br>")

    return f"""<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:#fff;border:1px solid #d9e1e6;border-radius:10px;margin:0 0 15px">
<tr><td style="padding:20px"><p style="margin:0 0 6px;color:#6b7c89;font-size:12px;font-weight:bold;text-transform:uppercase">Project {number}</p>
<h2 style="margin:0 0 15px;font-size:20px;line-height:1.35"><a href="{url}" style="color:#0b5c91;text-decoration:none">{html.escape(project['title'])}</a></h2>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="border-collapse:collapse;margin-bottom:15px">{facts}</table>
<p style="font-size:14px;line-height:1.6;color:#263746;margin:0 0 18px">{description or 'No description provided.'}</p>
<a href="{url}" style="display:inline-block;background:#0b5c91;color:#fff;text-decoration:none;padding:11px 16px;border-radius:6px;font-weight:bold">View exact project</a>
<p style="margin:12px 0 0;font-size:11px;word-break:break-all"><a href="{url}" style="color:#5d6d7e">{url}</a></p>
</td></tr></table>"""


def _fact_row(label: str, value: str) -> str:
    return (
        '<tr><td valign="top" style="width:135px;padding:6px 10px 6px 0;'
        'border-bottom:1px solid #edf1f3;color:#60717d;font-size:12px;'
        f'font-weight:bold">{html.escape(label)}</td>'
        '<td valign="top" style="padding:6px 0;border-bottom:1px solid #edf1f3;'
        f'color:#263746;font-size:13px">{html.escape(value)}</td></tr>'
    )


def _display_time(value: str) -> str:
    text = normalize_space(value)
    if not text:
        return "Not provided"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        zone = ZoneInfo(str(getattr(Config, "TIMEZONE", "UTC") or "UTC"))
    except ZoneInfoNotFoundError:
        zone = timezone.utc
    local = parsed.astimezone(zone)
    return local.strftime("%d %b %Y, %I:%M %p %Z")


def _digest_message_id(projects: Sequence[Mapping[str, str]]) -> str:
    material = "\n".join(
        sorted(f"{item['id']}|{item['url']}|{item['posted_at']}" for item in projects)
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return f"<freelancermap-{digest}@{_message_domain()}>"


def _random_message_id(prefix: str) -> str:
    seed = f"{prefix}|{datetime.now(timezone.utc).isoformat()}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
    return f"<{prefix}-{digest}@{_message_domain()}>"


def _message_domain() -> str:
    domain = _sender().rsplit("@", 1)[-1].casefold()
    safe = "".join(char for char in domain if char.isalnum() or char in ".-")
    return safe.strip(".-") or "localhost"


def _project_url(value: str) -> str:
    text = normalize_space(value)
    parsed = urlsplit(text)
    return text if parsed.scheme.lower() in {"http", "https"} and parsed.netloc else ""


def _email_description(value: str) -> str:
    text = normalize_space(value)
    limit = max(500, int(getattr(Config, "EMAIL_DESCRIPTION_MAX_CHARS", DEFAULT_DESCRIPTION_LIMIT)))
    if len(text) <= limit:
        return text
    return text[: limit - 34].rstrip() + " … [shortened in email]"


def _join_facts(*facts: tuple[str, str]) -> str:
    return " | ".join(
        f"{label}: {normalize_space(value)}"
        for label, value in facts
        if normalize_space(value)
    )


def _combine_text(*values: str) -> str:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = normalize_space(value)
        key = text.casefold()
        if text and key not in seen:
            output.append(text)
            seen.add(key)
    return "\n\n".join(output)


def _value(row: Row | Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        raw: Any = None
        if isinstance(row, Mapping):
            raw = row.get(key)
        elif isinstance(row, Row) and key in row.keys():
            raw = row[key]
        elif hasattr(row, key):
            raw = getattr(row, key)
        text = normalize_space(str(raw or ""))
        if text:
            return text
    return ""


def _smtp_text(value: bytes | str) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)


def _truncate(value: str, limit: int) -> str:
    """Compatibility helper retained for older tests/imports."""

    text = normalize_space(value)
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"