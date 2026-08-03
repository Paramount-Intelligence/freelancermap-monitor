from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import socket
import time
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, BinaryIO, Iterator
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

# HTML frequently contains these invisible formatting characters.  They are not
# matched consistently by ``\s`` and can make otherwise identical text compare
# differently, which in turn can create false content changes and duplicate
# alerts.
_INVISIBLE_TEXT_RE = re.compile("[\u00ad\u200b-\u200d\u2060\ufeff]")
_WHITESPACE_RE = re.compile(r"\s+")
_PERCENT_ESCAPE_RE = re.compile(r"%[0-9a-fA-F]{2}")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

# SystemRandom prevents an unrelated random.seed() call elsewhere in the
# process from making request jitter deterministic across monitor instances.
_JITTER_RANDOM = random.SystemRandom()


class URLNormalizationError(ValueError):
    """Raised when a URL cannot be normalized safely."""


def utc_now_iso(*, timespec: str = "seconds") -> str:
    """Return the current UTC time as a timezone-aware ISO 8601 string.

    The default omits microseconds so timestamps remain compact and stable for
    SQLite ordering, logs, email receipts, and fixture comparisons.
    """

    return datetime.now(timezone.utc).isoformat(timespec=timespec)


def local_now_display(timezone_name: str, *, now: datetime | None = None) -> str:
    """Return a human-readable local timestamp for email and console output.

    ``timezone_name`` must be an IANA time-zone key such as ``Asia/Karachi``.
    Supplying ``now`` is useful for deterministic tests; naive values are
    interpreted as UTC rather than as the host machine's local time.
    """

    zone_name = normalize_space(timezone_name)
    if not zone_name:
        raise ValueError("timezone_name must not be empty")

    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)

    return instant.astimezone(ZoneInfo(zone_name)).strftime(
        "%d %b %Y, %I:%M %p %Z"
    )


def normalize_space(value: str | None) -> str:
    """Collapse Unicode whitespace and remove invisible formatting marks.

    This helper is intentionally conservative: it does not change case,
    punctuation, or visible characters, so project descriptions remain faithful
    to the source page.
    """

    if value is None:
        return ""

    text = str(value)
    if not text:
        return ""

    text = _INVISIBLE_TEXT_RE.sub("", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def canonicalize_url(url: str, base_url: str) -> str:
    """Resolve and normalize one HTTP(S) URL for stable deduplication.

    Normalization deliberately removes query strings and fragments because
    Freelancermap tracking parameters do not identify a different project.  It
    lowercases the scheme and host, removes default ports, preserves the path's
    case and percent-encoding semantics, and removes a trailing slash except at
    the site root.

    The function rejects credentials, control characters, unsupported schemes,
    malformed ports, and hostless URLs.  It does *not* enforce same-origin
    policy; callers such as the parser and monitor can compare the normalized
    host with ``Config.BASE_URL`` and reject cross-origin links explicitly.
    """

    candidate = _clean_url_input(url, field_name="url")
    base = _clean_url_input(base_url, field_name="base_url")

    base_parts = _split_http_url(base, field_name="base_url")
    if not base_parts.netloc:
        raise URLNormalizationError("base_url must be an absolute HTTP(S) URL")
    _validated_hostname_and_port(base_parts, field_name="base_url")
    if base_parts.username is not None or base_parts.password is not None:
        raise URLNormalizationError("base_url must not contain embedded credentials")

    # A trailing slash makes a bare host behave as a directory base while not
    # altering a configured path such as /projects.
    join_base = base if base.endswith("/") else f"{base}/"
    absolute = urljoin(join_base, candidate)
    parts = _split_http_url(absolute, field_name="url")

    if parts.username is not None or parts.password is not None:
        raise URLNormalizationError("URLs containing embedded credentials are not allowed")

    hostname, port = _validated_hostname_and_port(parts, field_name="url")
    normalized_host = _normalize_hostname(hostname)
    if ":" in normalized_host and not normalized_host.startswith("["):
        normalized_host = f"[{normalized_host}]"

    scheme = parts.scheme.casefold()
    default_port = 443 if scheme == "https" else 80
    netloc = normalized_host if port in (None, default_port) else f"{normalized_host}:{port}"

    path = parts.path or "/"
    path = _normalize_percent_escape_case(path)
    if path != "/":
        path = path.rstrip("/") or "/"

    return urlunsplit((scheme, netloc, path, "", ""))


def ensure_query_param(url: str, name: str, value: str) -> str:
    """Return the URL with ``name=value`` present exactly once in its query.

    The parameter is appended when missing or replaced when already present,
    so a feed URL always carries exactly one copy of the configured sort key.
    """
    raw = _clean_url_input(url, field_name="url")
    parts = urlsplit(raw)
    query = parts.query
    if not name:
        return urlunsplit(parts)
    prefix = f"{name}="
    entries = [entry for entry in query.split("&") if entry and not entry.startswith(prefix)]
    entries.append(f"{name}={value}")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "&".join(entries), parts.fragment))


def source_key_from_url(url: str) -> str:
    """Return the final path segment, or a deterministic URL hash fallback.

    Freelancermap project URLs use the project slug as the stable identity.  A
    SHA-256 fallback keeps this helper total for root URLs and unusual fixtures
    without introducing an empty database key.
    """

    raw = _clean_url_input(url, field_name="url")
    parts = urlsplit(raw)
    segments = [segment for segment in parts.path.split("/") if segment]

    if segments:
        slug = unquote(segments[-1], errors="strict").strip()
        slug = _INVISIBLE_TEXT_RE.sub("", slug)
        if slug and slug not in {".", ".."} and not _CONTROL_RE.search(slug):
            return slug

    normalized_fallback = urlunsplit(
        (
            parts.scheme.casefold(),
            parts.netloc.casefold(),
            _normalize_percent_escape_case(parts.path.rstrip("/") or "/"),
            "",
            "",
        )
    )
    return hashlib.sha256(normalized_fallback.encode("utf-8")).hexdigest()


def stable_hash(payload: Any) -> str:
    """Return a deterministic SHA-256 digest for JSON-compatible content.

    Dictionary keys are sorted, insignificant JSON whitespace is removed, and
    common Python value types receive stable encodings.  Unsupported objects
    raise ``TypeError`` instead of falling back to ``str(object)``, which can
    include process-specific memory addresses and produce unstable hashes.
    """

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=_json_default,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def json_dumps(value: Any) -> str:
    """Serialize a value as compact, deterministic, UTF-8-friendly JSON."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=_json_default,
    )


def polite_sleep(min_seconds: float, max_seconds: float) -> None:
    """Sleep for a uniformly jittered delay within the supplied bounds.

    Negative bounds are clamped to zero, reversed bounds are accepted, and
    non-finite values are rejected.  This is request pacing, not a substitute
    for respecting the site's terms, robots policy, or explicit rate limits.
    """

    try:
        first = float(min_seconds)
        second = float(max_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("sleep bounds must be finite numbers") from exc

    if not math.isfinite(first) or not math.isfinite(second):
        raise ValueError("sleep bounds must be finite numbers")

    low, high = sorted((max(0.0, first), max(0.0, second)))
    delay = low if low == high else _JITTER_RANDOM.uniform(low, high)
    time.sleep(delay)


@contextmanager
def exclusive_file_lock(
    path: Path,
    *,
    timeout_seconds: float = 0.0,
    poll_interval_seconds: float = 0.1,
) -> Iterator[None]:
    """Acquire an exclusive cross-process lock backed by a local file.

    The default is non-blocking, matching the monitor's requirement that a
    second scheduled process fail immediately instead of running concurrently.
    A positive timeout can be used by maintenance commands.  The lock file is
    intentionally retained after release: deleting it can create an inode race
    in which two processes lock different files with the same path.
    """

    lock_path = Path(path).expanduser().resolve(strict=False)
    timeout = _finite_nonnegative(timeout_seconds, "timeout_seconds")
    poll_interval = _finite_nonnegative(
        poll_interval_seconds,
        "poll_interval_seconds",
    )
    if timeout > 0 and poll_interval == 0:
        raise ValueError("poll_interval_seconds must be greater than zero when waiting")

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = _open_lock_file(lock_path)
    deadline = time.monotonic() + timeout
    acquired = False

    try:
        _ensure_lock_byte(handle)

        while True:
            try:
                _lock_file(handle)
                acquired = True
                break
            except OSError as exc:
                if timeout <= 0 or time.monotonic() >= deadline:
                    owner = _read_lock_metadata(handle)
                    owner_text = f" Current lock metadata: {owner}." if owner else ""
                    raise RuntimeError(
                        "Another Freelancermap Monitor process is already running "
                        f"(lock: {lock_path}).{owner_text}"
                    ) from exc
                time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))

        _write_lock_metadata(handle)
        yield
    finally:
        try:
            if acquired:
                _unlock_file(handle)
        finally:
            handle.close()


def _clean_url_input(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")

    cleaned = value.strip()
    if not cleaned:
        raise URLNormalizationError(f"{field_name} must not be empty")
    if _CONTROL_RE.search(cleaned):
        raise URLNormalizationError(f"{field_name} contains control characters")
    if "\\" in cleaned:
        # Backslashes are interpreted inconsistently across URL parsers and
        # browsers.  Rejecting them avoids ambiguous host/path boundaries.
        raise URLNormalizationError(f"{field_name} contains a backslash")
    return cleaned


def _split_http_url(value: str, *, field_name: str):
    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise URLNormalizationError(f"{field_name} is malformed: {value!r}") from exc

    if parts.scheme.casefold() not in {"http", "https"}:
        raise URLNormalizationError(
            f"{field_name} must use http or https, got {parts.scheme or '(missing scheme)'!r}"
        )
    return parts



def _validated_hostname_and_port(parts: Any, *, field_name: str) -> tuple[str, int | None]:
    try:
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise URLNormalizationError(f"{field_name} contains an invalid host or port") from exc

    if not hostname:
        raise URLNormalizationError(f"{field_name} must contain a hostname")
    return hostname, port

def _normalize_hostname(hostname: str) -> str:
    value = hostname.rstrip(".").casefold()
    if not value:
        raise URLNormalizationError("URL hostname must not be empty")

    # IPv6 literals contain colons and are not IDNA names.
    if ":" in value:
        return value

    try:
        return value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise URLNormalizationError(f"URL contains an invalid hostname: {hostname!r}") from exc


def _normalize_percent_escape_case(value: str) -> str:
    return _PERCENT_ESCAPE_RE.sub(lambda match: match.group(0).upper(), value)


def _json_default(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (set, frozenset)):
        # Sorting by each item's canonical JSON representation makes mixed but
        # serializable sets deterministic without relying on incomparable types.
        return sorted(
            value,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
                default=_json_default,
            ),
        )
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _finite_nonnegative(value: float, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite non-negative number") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return number


def _open_lock_file(path: Path) -> BinaryIO:
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    return os.fdopen(descriptor, "r+b", buffering=0)


def _ensure_lock_byte(handle: BinaryIO) -> None:
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        os.fsync(handle.fileno())
    handle.seek(0)


def _lock_file(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_lock_metadata(handle: BinaryIO) -> None:
    metadata = {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "acquired_at": utc_now_iso(),
    }
    payload = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8")
    handle.seek(0)
    handle.write(payload)
    handle.truncate()
    os.fsync(handle.fileno())
    handle.seek(0)


def _read_lock_metadata(handle: BinaryIO) -> str:
    try:
        handle.seek(0)
        raw = handle.read(2048)
        return raw.decode("utf-8", errors="replace").strip()
    except OSError:
        return ""