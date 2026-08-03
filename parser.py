from __future__ import annotations

import json
import re
from calendar import monthrange
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, NavigableString, Tag

from pagedetect import has_error_title
from utils import canonicalize_url, normalize_space, source_key_from_url, stable_hash, utc_now_iso

PROJECT_PATH_RE = re.compile(r"^/project/[^/?#]+/?$")
PROJECT_URL_IN_TEXT_RE = re.compile(r"(?:https?://[^\s'\"<>]+)?(/project/[^\s'\"<>?#)]+)", re.I)

# Values observed on the public feed and the authenticated account feed.  The
# labeled-field parser remains the primary source, so new values are preserved
# even when they are not present in this fallback list.
KNOWN_CONTRACT_TYPES = (
    "Agency contract (e.g. ANÜ)",
    "Project-based",
    "Freelance",
    "Permanent",
    "Fixed-term",
    "Temporary",
)
WORKPLACE_RE = re.compile(
    r"\b(?:On[- ]site|Hybrid|Remote|Home[- ]based|Fully remote|Partial remote|\d{1,3}%\s*remote)\b",
    re.I,
)
POSTED_RE = re.compile(
    r"\b(?:just now|moments? ago|today|yesterday|\d+\s+(?:second|minute|hour|day|week|month|year)s?\s+ago)\b",
    re.I,
)
PUBLISHED_ON_RE = re.compile(
    r"\bpublished\s+(?:on\s+)?\d{1,2}[./-]\d{1,2}[./-]\d{4}(?:\s*,?\s*\d{1,2}:\d{2}\s*(?:AM|PM)?)?",
    re.I,
)
PUBLISHED_RE = re.compile(
    r"Published\s+on\s+(\d{1,2}/\d{1,2}/\d{4}),?\s*(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm))",
    re.I,
)
VIEWS_RE = re.compile(r"\b(\d[\d,.]*)\s+views?\b", re.I)
ABSOLUTE_DATETIME_RE = re.compile(
    r"(\d{1,2}/\d{1,2}/\d{4}),?\s*(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm))",
    re.I,
)
CLOCK_TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")
EUROPEAN_DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
DURATION_RE = re.compile(
    r"\b(?:\d+\+?\s*(?:days?|weeks?|months?|years?)|"
    r"\d+(?:\s*[-–]\s*\d+)?\s*(?:days?|weeks?|months?|years?)(?:\s*\+)?|"
    r"ongoing|long[- ]term)\b",
    re.I,
)
_MONTHS_PAT = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
START_RE = re.compile(
    rf"\b(?:ASAP|as soon as possible|immediately|flexible|TBD|"
    rf"\d{{1,2}}[./-]\d{{1,2}}[./-](?:\d{{2}}|\d{{4}})|"
    rf"\d{{1,2}}\s+{_MONTHS_PAT}\s+\d{{4}}|"
    rf"{_MONTHS_PAT}\s+\d{{1,2}},?\s+\d{{4}}|"
    rf"Q[1-4]\s+\d{{4}}|\d{{1,2}}/\d{{4}}|"
    rf"(?:early|mid|end of)?\s*{_MONTHS_PAT}(?:/{_MONTHS_PAT})?(?:\s+\d{{4}})?)\b",
    re.I,
)

FOOTER_MARKERS = (
    "For freelancers",
    "For companies",
    "Help and support",
    "About freelancermap",
)
UI_NOISE = {
    "Change availability",
    "Save",
    "Apply now",
    "Apply",
    "Save to watchlist",
    "Report project",
    "Reason for reporting this project:",
    "The project is outdated",
    "The project description is unprofessional / This is not a valid project",
    "Incorrect contract type",
    "The hourly rate is unrealistic",
    "Get notified by email when new projects match this search",
}

FACT_LABELS: dict[str, tuple[str, ...]] = {
    "location": ("location", "project location", "work location"),
    "contract_type": ("contract type", "contract", "engagement type"),
    "start_date": ("start date", "start", "project start"),
    "duration": ("duration", "project duration", "term", "contract length", "project length"),
    "workload": ("workload", "work load", "hours", "weekly hours"),
    "workplace": ("workplace", "work model", "working model", "remote arrangement"),
    "rate": ("rate", "hourly rate", "daily rate", "budget", "pay rate"),
}
LABEL_TO_FIELD = {
    alias.casefold(): field_name
    for field_name, aliases in FACT_LABELS.items()
    for alias in aliases
}


@dataclass(slots=True)
class ProjectDiscovery:
    source_key: str
    slug: str
    url: str
    title_hint: str = ""
    company_hint: str = ""
    posted_text: str = ""
    view_count: int | None = None
    card_description: str = ""
    card_description_html: str = ""
    card_location: str = ""
    card_workplace: str = ""
    card_contract_type: str = ""
    card_duration: str = ""
    card_start_date: str = ""
    card_workload: str = ""
    card_rate: str = ""
    card_skills: list[str] = field(default_factory=list)
    card_text: str = ""
    card_html: str = ""
    card_hash: str = ""
    scan_at: str = ""
    posted_at: str = ""
    _seen_in_primary: bool = True
    _seen_in_personalized: bool = False
    _primary_position: int | None = None
    _personalized_position: int | None = None

    def finalize(self) -> "ProjectDiscovery":
        self.scan_at = _normalize_scan_at(self.scan_at)
        self.posted_at = parse_relative_posted_time(self.posted_text, self.scan_at)
        self.card_skills = _unique_strings(self.card_skills)
        # Deliberately exclude view_count and relative posted_text. They are
        # volatile and must not trigger a detail-page refresh every scan.
        self.card_hash = stable_hash(
            {
                "title": self.title_hint,
                "company": self.company_hint,
                "description": self.card_description,
                "location": self.card_location,
                "workplace": self.card_workplace,
                "contract_type": self.card_contract_type,
                "duration": self.card_duration,
                "start_date": self.card_start_date,
                "workload": self.card_workload,
                "rate": self.card_rate,
                "skills": self.card_skills,
            }
        )
        return self

    def parsed_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("card_html", None)
        return payload


@dataclass(slots=True)
class ProjectDetail:
    source_key: str
    slug: str
    url: str
    title: str = ""
    company: str = ""
    company_url: str = ""
    contact_person: str = ""
    location: str = ""
    city: str = ""
    country: str = ""
    workplace: str = ""
    remote_percent: str = ""
    contract_type: str = ""
    duration: str = ""
    start_date: str = ""
    workload: str = ""
    posted_text: str = ""
    view_count: int | None = None
    publication_text: str = ""
    published_at: str = ""
    valid_through: str = ""
    industry: str = ""
    skills: list[str] = field(default_factory=list)
    rate: str = ""
    description: str = ""
    description_html: str = ""
    application_url: str = ""
    is_closed: bool = False
    raw_metadata: dict[str, Any] = field(default_factory=dict)
    content_hash: str = ""
    scan_at: str = ""
    posted_at: str = ""
    project_length: str = ""
    budget: str = ""
    engagement_type: str = ""

    def finalize(self) -> "ProjectDetail":
        self.scan_at = _normalize_scan_at(self.scan_at)
        self.posted_at = _resolve_posted_at(
            self.published_at or self.publication_text or self.posted_text,
            self.scan_at,
        )
        self.project_length = _project_length_text(
            self.start_date,
            self.duration,
            self.workload,
        )
        self.budget = self.rate
        self.engagement_type = self.workload or self.contract_type
        self.skills = _unique_strings(self.skills)
        digest_fields = {
            "title": self.title,
            "company": self.company,
            "contact_person": self.contact_person,
            "location": self.location,
            "workplace": self.workplace,
            "contract_type": self.contract_type,
            "duration": self.duration,
            "start_date": self.start_date,
            "workload": self.workload,
            "posted_text": self.posted_text,
            "published_at": self.published_at,
            "skills": self.skills,
            "rate": self.rate,
            "description": self.description,
            "is_closed": self.is_closed,
            "posted_at": self.posted_at,
            "project_length": self.project_length,
            "budget": self.budget,
            "engagement_type": self.engagement_type,
        }
        self.content_hash = stable_hash(digest_fields)
        return self

    def parsed_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class NormalizedProject:
    """The minimal project record required by the SQLite/email workflow."""

    scan_at: str
    posted_at: str
    title: str
    description: str
    location: str
    project_length: str
    url: str
    budget: str
    engagement_type: str


def parse_listing_cards(
    html: str,
    base_url: str,
    scan_at: str | datetime | None = None,
) -> list[ProjectDiscovery]:
    """Parse rendered listing HTML into structured project-card records.

    The authenticated account page uses rich cards whose description is useful
    data in its own right.  This function therefore captures the whole card,
    not only its URL and title.  It accepts normal links plus common SPA data
    attributes/onclick routes.
    """

    resolved_scan_at = _normalize_scan_at(scan_at)
    soup = BeautifulSoup(html, "lxml")
    found: dict[str, ProjectDiscovery] = {}

    for node, absolute in _project_route_nodes(soup, base_url):
        key = source_key_from_url(absolute)
        # Skip links inside similar-projects sections to prevent leaking
        # detail-page cards into listing results.
        if any(
            isinstance(p, Tag) and p.get("data-testid") == "similar-projects"
            for p in node.parents
        ):
            continue
        card = _nearest_card(node)
        discovery = _parse_card(card or node, node, absolute, base_url)
        discovery.scan_at = resolved_scan_at
        discovery.finalize()
        existing = found.get(key)
        if existing:
            found[key] = _richer_discovery(existing, discovery)
        else:
            found[key] = discovery

    # Fallback: extract from JSON state embedded in script tags when DOM cards
    # yielded fewer results.  JSON state is conservative -- it only produces
    # records that lack card HTML and must not override richer DOM cards.
    state_discoveries = _extract_json_state(soup, base_url)
    if found:
        # DOM cards exist: merge JSON state fields into DOM cards but do not
        # create new projects from state-only entries.
        for state_discovery in state_discoveries:
            state_discovery.scan_at = resolved_scan_at
            state_discovery.finalize()
            key = state_discovery.source_key
            if key in found:
                found[key] = _richer_discovery(found[key], state_discovery)
    else:
        for state_discovery in state_discoveries:
            state_discovery.scan_at = resolved_scan_at
            state_discovery.finalize()
            key = state_discovery.source_key
            if key not in found:
                found[key] = state_discovery

    return list(found.values())


def parse_project_links(
    html: str,
    base_url: str,
    scan_at: str | datetime | None = None,
) -> list[ProjectDiscovery]:
    """Backward-compatible alias for :func:`parse_listing_cards`."""

    return parse_listing_cards(html, base_url, scan_at)


def parse_project_detail(
    html: str,
    url: str,
    base_url: str,
    scan_at: str | datetime | None = None,
) -> ProjectDetail:
    soup = BeautifulSoup(html, "lxml")
    json_ld_items = _json_ld_items(soup)
    job = _first_job_posting(json_ld_items)
    canonical = _canonical_from_sources(job, soup, url, base_url)
    key = source_key_from_url(canonical)
    detail = ProjectDetail(
        source_key=key,
        slug=key,
        url=canonical,
        scan_at=_normalize_scan_at(scan_at),
    )
    meta = _meta_tags(soup)

    parser_meta: dict[str, Any] = {
        "version": 2,
        "field_sources": {},
        "warnings": [],
        "detail_scope": "detail_page",
    }
    detail.raw_metadata = {"json_ld": json_ld_items, "meta": meta, "parser": parser_meta}
    _apply_json_ld(detail, job, base_url)

    for sp in soup.select('[data-testid="similar-projects"], [class*="similar-projects"]'):
        sp.decompose()

    active_modal = (
        soup.select_one(".search-result-modal.show")
        or soup.select_one('.modal[aria-hidden="false"]')
    )
    if active_modal:
        # Strip similar-project cards inside the modal so they don't leak
        # into duration or description.
        for sp in active_modal.select('[data-testid="similar-projects"]'):
            sp.decompose()
        modal_soup = BeautifulSoup(str(active_modal), "lxml")
        existing_main = modal_soup.find("main")
        if not existing_main:
            main_tag = modal_soup.new_tag("main")
            for child in list(modal_soup.children):
                main_tag.append(child)
            modal_soup.append(main_tag)
        _apply_visible_page(detail, modal_soup, base_url)
    else:
        _apply_visible_page(detail, soup, base_url)

    if not detail.title:
        page_title = soup.title.string if soup.title and soup.title.string else ""
        detail.title = normalize_space(meta.get("og:title") or meta.get("twitter:title") or page_title)
    if not detail.description:
        detail.description = normalize_space(meta.get("description") or meta.get("og:description"))

    if detail.title and has_error_title(detail.title):
        parser_meta["warnings"].append("missing_title")
        detail.title = ""
        detail.description = ""

    detail.is_closed = detail.is_closed or bool(
        re.search(
            r"already been closed|no longer accepts applications|project is closed|project has expired|"
            r"application period has ended|no longer available",
            soup.get_text(" ", strip=True),
            re.I,
        )
    )
    return detail.finalize()


def parse_relative_posted_time(
    text: str | None,
    scan_at: str | datetime | None,
) -> str:
    """Resolve a relative posting label to an ISO-8601 UTC timestamp.

    The required storage rule is deliberately conservative: when no reliable
    posting time can be parsed, the scan timestamp is returned.  Consequently
    callers never receive an empty ``posted_at`` value.
    """

    scanned = _as_utc_datetime(scan_at)
    value = normalize_space(text)
    if not value:
        return _iso_utc(scanned)

    lowered = value.casefold()
    if lowered in {"just now", "moment ago", "moments ago", "less than a minute ago", "today"}:
        return _iso_utc(scanned)
    if lowered == "yesterday":
        return _iso_utc(scanned - timedelta(days=1))
    if lowered == "an hour ago":
        return _iso_utc(scanned - timedelta(hours=1))
    if lowered == "a minute ago":
        return _iso_utc(scanned - timedelta(minutes=1))
    if lowered == "a day ago":
        return _iso_utc(scanned - timedelta(days=1))

    match = re.search(
        r"\b(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago\b",
        lowered,
    )
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        if unit == "second":
            posted = scanned - timedelta(seconds=amount)
        elif unit == "minute":
            posted = scanned - timedelta(minutes=amount)
        elif unit == "hour":
            posted = scanned - timedelta(hours=amount)
        elif unit == "day":
            posted = scanned - timedelta(days=amount)
        elif unit == "week":
            posted = scanned - timedelta(weeks=amount)
        elif unit == "month":
            posted = _subtract_calendar_months(scanned, amount)
        else:
            posted = _subtract_calendar_years(scanned, amount)
        return _iso_utc(posted)

    absolute = _parse_absolute_datetime(value, scanned)
    if absolute:
        if absolute > scanned + timedelta(minutes=5):
            return _iso_utc(scanned)
        return _iso_utc(absolute)
    return _iso_utc(scanned)


def merge_card_and_detail(
    card: ProjectDiscovery,
    detail: ProjectDetail | None,
    scan_at: str | datetime | None = None,
) -> NormalizedProject:
    """Merge card and detail provenance into the required minimal record.

    Detail-page values win when present, but card values remain valid
    fallbacks.  Descriptions are merged without exact or containment
    duplication so useful card-only text is not discarded.
    """

    resolved_scan_at = _normalize_scan_at(
        scan_at or (detail.scan_at if detail else "") or card.scan_at
    )
    resolved_detail = detail

    title = normalize_space(
        (resolved_detail.title if resolved_detail else "") or card.title_hint
    )
    description = _merge_descriptions(
        card.card_description,
        resolved_detail.description if resolved_detail else "",
    )
    location = normalize_space(
        (resolved_detail.location if resolved_detail else "") or card.card_location
    )
    start_date = normalize_space(
        (resolved_detail.start_date if resolved_detail else "") or card.card_start_date
    )
    duration = normalize_space(
        (resolved_detail.duration if resolved_detail else "") or card.card_duration
    )
    workload = normalize_space(
        (resolved_detail.workload if resolved_detail else "") or card.card_workload
    )
    project_length = _project_length_text(start_date, duration, workload)
    budget = normalize_space(
        (resolved_detail.rate if resolved_detail else "") or card.card_rate
    )
    engagement_type = normalize_space(
        (resolved_detail.workload if resolved_detail else "")
        or card.card_workload
        or (resolved_detail.contract_type if resolved_detail else "")
        or card.card_contract_type
    )
    exact_url = canonicalize_url(
        (resolved_detail.url if resolved_detail else "") or card.url,
        card.url,
    )

    posted_candidates = (
        resolved_detail.published_at if resolved_detail else "",
        resolved_detail.publication_text if resolved_detail else "",
        resolved_detail.posted_text if resolved_detail else "",
        card.posted_text,
    )
    posted_at = resolved_scan_at
    for candidate in posted_candidates:
        if normalize_space(candidate):
            posted_at = _resolve_posted_at(candidate, resolved_scan_at)
            break
    else:
        # ``finalize`` guarantees non-empty posted_at values by falling back to
        # scan_at. Preserve a precomputed value only when it is genuinely older
        # than the scan and therefore represents source posting information.
        for candidate, candidate_scan in (
            (
                resolved_detail.posted_at if resolved_detail else "",
                resolved_detail.scan_at if resolved_detail else "",
            ),
            (card.posted_at, card.scan_at),
        ):
            if candidate and candidate != candidate_scan:
                posted_at = _resolve_posted_at(candidate, resolved_scan_at)
                break

    return NormalizedProject(
        scan_at=resolved_scan_at,
        posted_at=posted_at,
        title=title,
        description=description,
        location=location,
        project_length=project_length,
        url=exact_url,
        budget=budget,
        engagement_type=engagement_type,
    )


def _extract_json_state(soup: BeautifulSoup, base_url: str) -> list[ProjectDiscovery]:
    """Extract project cards from JSON state embedded in script tags."""
    discoveries: list[ProjectDiscovery] = []
    for script in soup.find_all("script", attrs={"type": re.compile(r"application/json", re.I)}):
        component = script.get("data-component-name", "")
        if not re.search(r"projectsearch|project_search|projectSearch", component, re.I):
            continue
        raw = script.string or script.get_text("", strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        projects = []
        if isinstance(data, dict):
            projects = data.get("projects", [])
            if not projects:
                for sub_key in ("projectSearch", "project_search", "initialState", "state"):
                    sub = data.get(sub_key)
                    if isinstance(sub, dict):
                        projects = sub.get("projects", []) or (sub.get("result", {}).get("projects", []) if isinstance(sub.get("result"), dict) else [])
                        if projects:
                            break
        if not isinstance(projects, list):
            continue
        for project_data in projects:
            if not isinstance(project_data, dict):
                continue
            discovery = _parse_json_state_project(project_data, base_url)
            if discovery:
                discoveries.append(discovery)
    return discoveries


def _parse_json_state_project(data: dict, base_url: str) -> ProjectDiscovery | None:
    """Parse a single project from JSON state data."""
    project_url = ""
    links = data.get("links", {})
    if isinstance(links, dict):
        project_url = links.get("project", "")
    if not project_url:
        project_url = data.get("url", "") or data.get("href", "")
    if not project_url:
        project_url = data.get("projectUrl", "")
    if not project_url:
        return None
    absolute = canonicalize_url(project_url, base_url)
    key = source_key_from_url(absolute)
    if not key:
        return None

    title = data.get("title", "") or data.get("projectTitle", "") or ""
    company = data.get("company", "") or data.get("providerName", "") or ""
    description = data.get("description", "") or ""
    # Strip HTML tags to get plain text for card_description.
    if "<" in description:
        description = BeautifulSoup(description, "lxml").get_text(" ", strip=True)
    city = data.get("city", "")
    country = data.get("country", "")
    if isinstance(country, dict):
        country = country.get("name", "")
    location_parts = [city, country] if city else []
    location = ", ".join(part for part in location_parts if part)

    remote_info = data.get("projectContractType", {})
    remote_percent = data.get("remoteInPercent")
    contract_type_str = ""
    if isinstance(remote_info, dict):
        if remote_percent is None:
            remote_percent = remote_info.get("remoteInPercent", 0)
        contract_type_str = remote_info.get("type", "")
    workplace = ""
    if isinstance(remote_percent, str):
        workplace = normalize_space(remote_percent)
    elif isinstance(remote_percent, int) and remote_percent > 0:
        if remote_percent == 100:
            workplace = "100% remote"
        else:
            workplace = f"{remote_percent}% remote"
    elif isinstance(remote_info, str):
        workplace = remote_info

    # Map contract type string to known types
    contract_type = ""
    if contract_type_str:
        lowered = contract_type_str.casefold()
        if "contract" in lowered or "freelance" in lowered or "consulting" in lowered:
            contract_type = "Freelance"

    duration = data.get("durationText", "") or data.get("duration", "")
    start_date = data.get("beginningText", "") or data.get("startDate", "")
    created = data.get("created", "")
    view_count = data.get("viewCount")
    if isinstance(view_count, str):
        try:
            view_count = int(view_count)
        except ValueError:
            view_count = None

    return ProjectDiscovery(
        source_key=key,
        slug=key,
        url=absolute,
        title_hint=title,
        company_hint=company,
        card_description=description,
        card_location=location,
        card_workplace=workplace,
        card_contract_type=contract_type,
        card_duration=duration,
        card_start_date=start_date,
        posted_text=created,
        view_count=view_count,
    )


def _project_route_nodes(soup: BeautifulSoup, base_url: str) -> list[tuple[Tag, str]]:
    result: list[tuple[Tag, str]] = []
    seen: set[tuple[int, str]] = set()
    candidate_nodes = soup.find_all(
        lambda tag: isinstance(tag, Tag)
        and (
            tag.name == "a"
            or any(tag.has_attr(attr) for attr in ("data-href", "data-url", "formaction", "onclick"))
        )
    )
    for node in candidate_nodes:
        for candidate in _route_candidates(node):
            absolute = canonicalize_url(candidate, base_url)
            parsed = urlparse(absolute)
            if parsed.netloc.casefold() != urlparse(base_url).netloc.casefold():
                continue
            if not PROJECT_PATH_RE.match(parsed.path):
                continue
            marker = (id(node), absolute)
            if marker not in seen:
                seen.add(marker)
                result.append((node, absolute))
    return result


def _route_candidates(node: Tag) -> list[str]:
    candidates: list[str] = []
    for attribute in ("href", "data-href", "data-url", "formaction"):
        value = normalize_space(str(node.get(attribute) or ""))
        if value:
            candidates.append(value)
    onclick = str(node.get("onclick") or "")
    candidates.extend(match.group(1) for match in re.finditer(r"['\"]([^'\"]*/project/[^'\"]+)['\"]", onclick, re.I))
    return candidates


def _parse_card(card: Tag, route_node: Tag, absolute: str, base_url: str) -> ProjectDiscovery:
    key = source_key_from_url(absolute)
    lines = _visible_lines(card)
    card_text = normalize_space(card.get_text(" | ", strip=True))
    title = _card_title(card, route_node)
    title_index = _index_of(lines, title)
    facts = _extract_fact_map(card, lines)
    view_count = _extract_view_count(card_text)
    company = _card_company(card, lines, title_index, title)
    description, description_html = _card_description(card, lines, title_index, title, company)

    # Extract data-testid attributes for structured card fields.
    testid_map = _card_testid_map(card)
    created_text = testid_map.get("created", "")
    beginning_text = testid_map.get("beginningText", "")
    beginning_month = testid_map.get("beginningMonth", "")

    if created_text:
        posted_text = created_text
    else:
        card_copy = BeautifulSoup(str(card), "html.parser")
        for tag in card_copy.find_all(lambda t: hasattr(t, "attrs") and t.attrs):
            tid = str(tag.attrs.get("data-testid", "") or "").lower()
            cls = str(tag.attrs.get("class", "") or "")
            if isinstance(cls, list):
                cls = " ".join(cls)
            cls = cls.lower()
            if "description" in tid or "description" in cls or "startdate" in tid or "beginning" in tid or "title" in tid or "title" in cls:
                tag.decompose()
        clean_text = normalize_space(card_copy.get_text(" | ", strip=True))
        posted_text = _extract_posted_text(clean_text)

    location = facts.get("location", "") or testid_map.get("city", "") or _card_location(lines, title_index, company)
    workplace = facts.get("workplace", "") or testid_map.get("workplace", "") or _first_workplace(lines)
    contract_type = facts.get("contract_type", "") or testid_map.get("type", "") or _first_contract_type(lines)
    duration = facts.get("duration", "") or testid_map.get("duration", "") or _first_duration(lines)
    # Exclude the created date from start-date detection so it doesn't get misclassified.
    start_lines = [line for line in lines if not created_text or line != created_text]
    start_date = facts.get("start_date", "") or beginning_text or beginning_month or _first_start(start_lines, duration)
    workload = facts.get("workload", "") or testid_map.get("workload", "")
    rate = facts.get("rate", "") or _extract_rate(card_text)

    # The created date is the posting date, not a start date.
    if created_text and not posted_text:
        posted_text = created_text


    return ProjectDiscovery(
        source_key=key,
        slug=key,
        url=absolute,
        title_hint=title,
        company_hint=company,
        posted_text=posted_text,
        view_count=view_count,
        card_description=description,
        card_description_html=description_html,
        card_location=location,
        card_workplace=workplace,
        card_contract_type=contract_type,
        card_duration=duration,
        card_start_date=start_date,
        card_workload=workload,
        card_rate=rate,
        card_skills=_extract_skill_links(card, base_url),
        card_text=card_text,
        card_html=str(card),
    )


def _card_testid_map(card: Tag) -> dict[str, str]:
    """Extract data-testid attributes from a card element."""
    result: dict[str, str] = {}
    for node in card.find_all(attrs={"data-testid": True}):
        testid = str(node.get("data-testid", ""))
        text = normalize_space(node.get_text(" ", strip=True))
        if testid and text:
            result[testid] = text
    return result


def _richer_discovery(left: ProjectDiscovery, right: ProjectDiscovery) -> ProjectDiscovery:
    """Merge two discoveries. left (DOM card) takes priority for shared fields."""
    # Determine which has richer card HTML (DOM vs state-generated).
    def has_dom(item: ProjectDiscovery) -> bool:
        return bool(item.card_html)

    # Use DOM card as the winner when present; otherwise use richer score.
    if has_dom(left):
        winner, other = left, right
    elif has_dom(right):
        winner, other = right, left
    else:
        def score(item: ProjectDiscovery) -> tuple[int, int, int]:
            populated = sum(
                bool(value)
                for value in (
                    item.title_hint,
                    item.company_hint,
                    item.card_description,
                    item.card_location,
                    item.card_workplace,
                    item.card_contract_type,
                    item.card_duration,
                    item.card_start_date,
                    item.card_workload,
                    item.card_rate,
                )
            )
            return populated, len(item.card_description), len(item.card_text)
        winner, other = (right, left) if score(right) > score(left) else (left, right)

    # Fields where the state data takes priority over DOM card data.
    STATE_PRIORITY_FIELDS = {"posted_text"}
    for field_name in (
        "title_hint",
        "company_hint",
        "posted_text",
        "card_description",
        "card_description_html",
        "card_location",
        "card_workplace",
        "card_contract_type",
        "card_duration",
        "card_start_date",
        "card_workload",
        "card_rate",
        "card_text",
        "card_html",
    ):
        if field_name in STATE_PRIORITY_FIELDS:
            if not getattr(other, field_name) and getattr(winner, field_name):
                pass  # keep winner's value
            elif getattr(other, field_name):
                setattr(winner, field_name, getattr(other, field_name))
        else:
            if not getattr(winner, field_name) and getattr(other, field_name):
                setattr(winner, field_name, getattr(other, field_name))
    if winner.view_count is None:
        winner.view_count = other.view_count
    winner.card_skills = _unique_strings([*winner.card_skills, *other.card_skills])
    return winner.finalize()


def _nearest_card(node: Tag) -> Tag | None:
    node_classes = " ".join(node.get("class", []))
    node_identity = " ".join(str(node.get(x) or "") for x in ("id", "data-testid", "data-test"))
    if node.name in {"article", "li"} or (
        node.name not in {"a", "button", "input"}
        and re.search(
            r"\bproject-card\b(?![\w-])|\blisting\b|\bresult\b|\bteaser\b|\bopportunity\b",
            f"{node_classes} {node_identity}",
            re.I,
        )
    ):
        return node

    fallback: Tag | None = None
    for depth, parent in enumerate(node.parents):
        if not isinstance(parent, Tag) or parent.name in {"body", "main", "html"}:
            break
        text = normalize_space(parent.get_text(" ", strip=True))
        if not text or len(text) > 6000:
            continue
        fallback = fallback or parent
        classes = " ".join(parent.get("class", []))
        identity = " ".join(str(parent.get(x) or "") for x in ("id", "data-testid", "data-test"))
        if parent.name in {"article", "li"} or re.search(
            r"\bproject-card\b(?![\w-])|\blisting\b|\bresult\b|\bteaser\b|\bopportunity\b", f"{classes} {identity}", re.I
        ):
            return parent
        if depth >= 8:
            break
    return fallback


def _card_title(card: Tag, route_node: Tag) -> str:
    generic = {"view project", "read more", "details", "apply now", "apply"}
    route_text = normalize_space(route_node.get_text(" ", strip=True))
    if route_text and route_text.casefold() not in generic and len(route_text) <= 300:
        return route_text
    for selector in ("h1", "h2", "h3", "h4", "[class*='title']", "[data-testid*='title']"):
        node = card.select_one(selector)
        if node:
            text = normalize_space(node.get_text(" ", strip=True))
            if text and text.casefold() not in generic:
                return text
    return route_text


def _card_company(card: Tag, lines: list[str], title_index: int, title: str) -> str:
    for selector in (
        "[data-testid*='company']",
        "[data-testid*='provider']",
        "[class*='company']",
        "[class*='provider']",
        "[class*='client']",
    ):
        node = card.select_one(selector)
        if node:
            text = normalize_space(node.get_text(" ", strip=True))
            if text and text != title and not _is_noise_line(text):
                return text

    start = title_index + 1 if title_index >= 0 else 0
    # Also check lines before the title — the company/provider often appears first.
    candidates = (lines[:title_index] if title_index > 0 else []) + lines[start : start + 8]
    for line in candidates:
        if line == title or _is_noise_line(line) or _is_metadata_line(line) or _is_fact_label(line):
            continue
        if _looks_like_fact_value(line) or len(line) > 160:
            continue
        # Skip lines that look like locations (city, country).
        if "," in line and not line.endswith("Provider") and not line.endswith("Inc"):
            continue
        return line
    return ""


def _card_description(
    card: Tag, lines: list[str], title_index: int, title: str, company: str
) -> tuple[str, str]:
    for selector in (
        "[data-testid*='description']",
        "[data-testid*='summary']",
        "[class*='description']",
        "[class*='summary']",
        "[class*='excerpt']",
        "[class*='snippet']",
    ):
        node = card.select_one(selector)
        if node:
            text = _multiline_text(node)
            if text and text not in {title, company}:
                return text, str(node)

    paragraphs = []
    paragraph_html = []
    for node in card.find_all("p"):
        text = _multiline_text(node)
        if text and text not in {title, company} and not _is_metadata_line(text):
            paragraphs.append(text)
            paragraph_html.append(str(node))
    if paragraphs:
        return "\n".join(paragraphs), "\n".join(paragraph_html)

    start = title_index + 1 if title_index >= 0 else 0
    candidates: list[str] = []
    for line in lines[start:]:
        if line in {title, company} or _is_noise_line(line) or _is_metadata_line(line):
            continue
        if _is_fact_label(line) or _looks_like_fact_value(line):
            continue
        if len(line) >= 35:
            candidates.append(line)
    return "\n".join(dict.fromkeys(candidates)), ""


def _card_location(lines: list[str], title_index: int, company: str) -> str:
    start = title_index + 1 if title_index >= 0 else 0
    for line in lines[start : start + 12]:
        if line == company or _is_metadata_line(line) or _is_noise_line(line):
            continue
        if line.casefold() == "remote" or ("," in line and len(line) <= 120):
            return line
    return ""


def _canonical_from_sources(
    job: dict[str, Any], soup: BeautifulSoup, current_url: str, base_url: str
) -> str:
    candidates: list[str] = []
    job_url = _as_text(job.get("url")) if job else ""
    if not job_url and job:
        main_entity = job.get("mainEntityOfPage")
        if isinstance(main_entity, dict):
            job_url = _as_text(main_entity.get("@id") or main_entity.get("url"))
        else:
            job_url = _as_text(main_entity)
    if job_url:
        candidates.append(job_url)

    canonical = soup.find("link", rel=lambda value: value and "canonical" in value)
    if canonical and canonical.get("href"):
        candidates.append(str(canonical["href"]))
    candidates.append(current_url)

    base_host = urlparse(base_url).netloc.casefold()
    for candidate in candidates:
        normalized = canonicalize_url(candidate, base_url)
        parsed = urlparse(normalized)
        if parsed.netloc.casefold() == base_host and PROJECT_PATH_RE.match(parsed.path):
            return normalized
    return canonicalize_url(current_url, base_url)


def _json_ld_items(soup: BeautifulSoup) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}):
        raw = script.string or script.get_text("", strip=True)
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        output.extend(_flatten_json_ld(parsed))
    return output


def _flatten_json_ld(value: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if isinstance(value, dict):
        graph = value.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                items.extend(_flatten_json_ld(item))
        items.append(value)
    elif isinstance(value, list):
        for item in value:
            items.extend(_flatten_json_ld(item))
    return items


def _first_job_posting(items: Iterable[dict[str, Any]]) -> dict[str, Any]:
    job_postings = []
    for item in items:
        kind = item.get("@type")
        kinds = kind if isinstance(kind, list) else [kind]
        if any(str(value).casefold() == "jobposting" for value in kinds):
            job_postings.append(item)
    if not job_postings:
        return {}
    # Prefer the entry with the most populated fields (description, datePosted, etc.)
    def _score(item: dict) -> int:
        return sum(1 for key in ("title", "description", "datePosted", "url", "employmentType") if item.get(key))
    return max(job_postings, key=_score)


def _meta_tags(soup: BeautifulSoup) -> dict[str, str]:
    result: dict[str, str] = {}
    for tag in soup.find_all("meta"):
        key = tag.get("property") or tag.get("name")
        value = tag.get("content")
        if key and value:
            result[str(key).casefold()] = normalize_space(str(value))
    return result


def _apply_json_ld(detail: ProjectDetail, job: dict[str, Any], base_url: str) -> None:
    if not job:
        return
    detail.title = normalize_space(_as_text(job.get("title")))
    raw_description = job.get("description")
    if isinstance(raw_description, list):
        raw_description = "\n".join(_as_text(item) for item in raw_description if _as_text(item))
    detail.description_html = _as_text(raw_description)
    detail.description = _multiline_text(BeautifulSoup(detail.description_html, "lxml"))
    detail.published_at = normalize_space(_as_text(job.get("datePosted")))
    detail.publication_text = detail.publication_text or detail.published_at
    detail.valid_through = normalize_space(_as_text(job.get("validThrough")))
    detail.contract_type = normalize_space(_as_text(job.get("employmentType")))
    # Handle employmentType as a list (e.g., ["FULL_TIME", "CONTRACTOR"])
    employment_type = job.get("employmentType")
    if isinstance(employment_type, list):
        mapped = []
        for etype in employment_type:
            text = _as_text(etype).upper()
            if "FULL" in text or "PERMANENT" in text:
                mapped.append("Full-time")
            elif "PART" in text:
                mapped.append("Part-time")
            elif "CONTRACT" in text or "FREELANCE" in text:
                mapped.append("Freelance")
        if mapped:
            detail.workload = detail.workload or mapped[0]
            detail.contract_type = detail.contract_type or (mapped[-1] if len(mapped) > 1 else mapped[0])
    detail.industry = normalize_space(_as_text(job.get("industry")))

    org = job.get("hiringOrganization")
    if isinstance(org, dict):
        detail.company = normalize_space(_as_text(org.get("name")))
        company_url = _as_text(org.get("sameAs") or org.get("url"))
        detail.company_url = urljoin(base_url + "/", company_url) if company_url else ""

    # Handle jobLocationType for remote detection
    job_location_type = _as_text(job.get("jobLocationType")).upper()
    is_remote_type = "TELECOMMUTE" in job_location_type or "REMOTE" in job_location_type

    job_location = job.get("jobLocation")
    if isinstance(job_location, list) and len(job_location) > 1:
        # Multiple locations: first is primary, rest are alternate
        primary = job_location[0]
        alternates = []
        for loc in job_location[1:]:
            if isinstance(loc, dict):
                addr = loc.get("address", {})
                if isinstance(addr, dict):
                    city = normalize_space(_as_text(addr.get("addressLocality")))
                    country = normalize_space(_country_text(addr.get("addressCountry")))
                    parts = [part for part in (city, country) if part]
                    if parts:
                        alternates.append(", ".join(parts))
        if alternates:
            detail.raw_metadata["alternate_locations"] = alternates
        job_location = primary
    else:
        job_location = job_location[0] if isinstance(job_location, list) and job_location else job_location

    if isinstance(job_location, dict):
        address = job_location.get("address")
        if isinstance(address, dict):
            detail.city = normalize_space(_as_text(address.get("addressLocality")))
            detail.country = normalize_space(_country_text(address.get("addressCountry")))
            parts = [detail.city, normalize_space(_as_text(address.get("addressRegion"))), detail.country]
            detail.location = ", ".join(value for value in parts if value)
        elif address:
            detail.location = normalize_space(_as_text(address))

    if is_remote_type and not detail.workplace:
        detail.workplace = "Remote"

    detail.skills.extend(_split_skills(job.get("skills")))
    detail.skills.extend(_split_skills(job.get("qualifications")))

    base_salary = job.get("baseSalary")
    if base_salary:
        detail.rate = normalize_space(_salary_text(base_salary))

    if job.get("directApply") is True:
        detail.application_url = detail.url


def _apply_visible_page(detail: ProjectDetail, soup: BeautifulSoup, base_url: str) -> None:
    main = soup.find("main") or soup.body or soup
    lines = _visible_lines(main)
    title_node = main.find("h1")
    if title_node:
        visible_title = normalize_space(title_node.get_text(" ", strip=True))
        # The rendered heading is what the user sees and therefore wins over
        # stale structured metadata.
        detail.title = visible_title or detail.title

    title_index = _index_of(lines, detail.title)
    description_index = _index_casefold(lines, "Description")
    pre_lines = (
        lines[title_index + 1 : description_index if description_index >= 0 else len(lines)]
        if title_index >= 0
        else lines[: description_index if description_index >= 0 else len(lines)]
    )

    detail.posted_text = _extract_posted_text(" ".join(pre_lines))
    if not detail.posted_text and title_index >= 0:
        # The date may appear before the title (e.g., modal header).
        detail.posted_text = _extract_posted_text(" ".join(lines[:title_index]))
    if not detail.posted_text:
        for line in lines:
            pub_match = PUBLISHED_RE.search(line)
            if pub_match:
                detail.posted_text = normalize_space(pub_match.group(0))
                break
    detail.view_count = _extract_view_count(" ".join(pre_lines))

    field_sources: dict[str, str] = {}
    if title_node and detail.title:
        field_sources["title"] = "visible_detail"

    facts = _extract_fact_map(main, lines)
    for field in ("location", "contract_type", "start_date", "duration", "workload", "workplace", "rate"):
        if facts.get(field):
            setattr(detail, field, facts[field])
            field_sources[field] = "visible_detail"

    useful = [line for line in pre_lines if not _is_noise_line(line) and not _is_report_noise(line)]
    for line in useful:
        if line.casefold().startswith("contact person:"):
            detail.contact_person = normalize_space(line.split(":", 1)[1])

    # Fallback: extract contact person from structured class (modal layout)
    if not detail.contact_person:
        info_name = main.select_one(".project-info-name")
        if info_name:
            detail.contact_person = normalize_space(info_name.get_text(" ", strip=True))

    contact_idx = next((i for i, value in enumerate(useful) if value.casefold().startswith("contact person:")), -1)
    if contact_idx >= 0 and not detail.location:
        loc_candidate = _extract_location_from_lines(useful[contact_idx + 1:])
        if loc_candidate:
            detail.location = loc_candidate
            field_sources["location"] = "visible_detail"

    combined_facts = " ".join(useful)
    if not detail.workplace:
        wp = _first_workplace(useful)
        if wp:
            detail.workplace = wp
            field_sources["workplace"] = "visible_detail"
        else:
            # Fall back to searching all lines for workplace keywords
            wp = _first_workplace(lines)
            if wp:
                detail.workplace = wp
                field_sources["workplace"] = "visible_detail"
    if not detail.contract_type:
        ct = _first_contract_type(useful)
        if ct:
            detail.contract_type = ct
            field_sources["contract_type"] = "visible_detail"
    if not detail.duration:
        for dur_label in ("Duration", "Contract Length", "Project Length"):
            raw = _labeled_value(combined_facts, dur_label, ("Start date", "Workload", "Workplace", "Rate", "Contract type"))
            if raw and not _is_prose_value(raw):
                detail.duration = _clean_fact_value("duration", raw)
                field_sources["duration"] = "visible_detail"
                break
    if not detail.start_date:
        raw = _labeled_value(combined_facts, "Start date", ("Duration", "Workload", "Workplace", "Rate", "Contract type"))
        if raw and not _is_prose_value(raw):
            detail.start_date = _clean_fact_value("start_date", raw)
            field_sources["start_date"] = "visible_detail"
        else:
            start_lines = [line for line in useful if not _is_noise_line(line)]
            fallback_start = _first_start(start_lines, detail.duration)
            if fallback_start:
                detail.start_date = fallback_start
                field_sources["start_date"] = "visible_detail"
    if not detail.workload:
        raw = _labeled_value(combined_facts, "Workload", ("Duration", "Start date", "Workplace", "Rate", "Contract type"))
        if raw and not _is_prose_value(raw):
            detail.workload = _clean_fact_value("workload", raw)
            field_sources["workload"] = "visible_detail"
        else:
            workload_match = re.search(r"\b(\d{1,3}%\s*workload)\b", combined_facts, re.I)
            if workload_match:
                detail.workload = normalize_space(workload_match.group(1))
                field_sources["workload"] = "visible_detail"
    if not detail.rate:
        r = _extract_rate(" ".join(useful))
        if r and not _is_prose_value(r):
            detail.rate = r
            field_sources["rate"] = "visible_detail"

    if detail.workplace:
        percent = re.search(r"\b\d{1,3}%\s*remote\b", detail.workplace, re.I)
        if percent:
            detail.remote_percent = normalize_space(percent.group(0))
    if not detail.remote_percent:
        percent = re.search(r"\b\d{1,3}%\s*remote\b", " ".join(lines), re.I)
        if percent:
            detail.remote_percent = normalize_space(percent.group(0))

    if not detail.location:
        detail.location = _extract_location_from_lines(useful)
        if detail.location:
            field_sources["location"] = "visible_detail"
    _split_location(detail)

    description, description_html = _extract_description(soup, lines, description_index, detail.title)
    if description:
        # Visible detail text wins over JSON-LD because it preserves the exact
        # current page content and headings shown to the user.
        detail.description = description
        detail.description_html = description_html

    # Extract location from description body if not found in pre-description lines or if header location is generic.
    if (not detail.location or detail.location.casefold() in {"not specified", "worldwide"}) and description:
        desc_loc = _labeled_value(description, "Location", ("Duration", "Workload", "Workplace", "Rate", "Contract type", "Description"))
        if desc_loc and not _is_prose_value(desc_loc):
            detail.location = _clean_fact_value("location", desc_loc)
            field_sources["location"] = "visible_detail"
            detail.city = ""
            detail.country = ""
            _split_location(detail)

    # Extract remaining fields from description body if not found in pre-description lines.
    DESC_STOPS = ("Duration", "Start", "Start date", "Workload", "Workplace", "Work location", "Rate", "Hourly rate", "Contract type", "Location", "Description", "Responsibilities", "Requirements")
    if description and not detail.duration:
        for dur_label in ("Duration", "Contract Length", "Project Length"):
            raw = _labeled_value(description, dur_label, DESC_STOPS)
            if raw and not _is_prose_value(raw):
                detail.duration = _clean_fact_value("duration", raw)
                field_sources["duration"] = "visible_detail"
                break
    if description and not detail.workload:
        wl_match = re.search(r"\b(\d{1,3}%\s*workload)\b", description, re.I)
        if wl_match:
            detail.workload = normalize_space(wl_match.group(1))
            field_sources["workload"] = "visible_detail"
        else:
            raw = _labeled_value(description, "Workload", DESC_STOPS)
            if raw and not _is_prose_value(raw):
                detail.workload = _clean_fact_value("workload", raw)
                field_sources["workload"] = "visible_detail"
    if description and not detail.contract_type:
        ct = _first_contract_type([description])
        if ct:
            detail.contract_type = ct
            field_sources["contract_type"] = "visible_detail"
    if description and not detail.start_date:
        for start_label in ("Start date", "Start", "Project start"):
            stops = tuple(s for s in DESC_STOPS if s.casefold() != start_label.casefold())
            raw = _labeled_value(description, start_label, stops)
            if raw and not _is_prose_value(raw):
                cleaned = _clean_fact_value("start_date", raw)
                if cleaned:
                    detail.start_date = cleaned
                    field_sources["start_date"] = "visible_detail"
                    break
    if description and not detail.rate:
        r = _extract_rate(description)
        if r and not _is_prose_value(r):
            detail.rate = r
            field_sources["rate"] = "visible_detail"

    detail.skills.extend(_extract_skill_links(soup, base_url))

    apply_target = _find_apply_target(soup)
    if apply_target:
        detail.application_url = urljoin(base_url + "/", apply_target)
    elif _has_apply_control(soup):
        detail.application_url = detail.application_url or detail.url

    visible_company = _visible_company(main, title_node, useful, detail.title)
    if visible_company:
        detail.company = visible_company
    company_link = _company_link_after_title(title_node)
    if company_link:
        detail.company = detail.company or normalize_space(company_link.get_text(" ", strip=True))
        if company_link.get("href"):
            detail.company_url = urljoin(base_url + "/", company_link["href"])

    if not detail.publication_text:
        detail.publication_text = detail.posted_text or _extract_absolute_date(" ".join(lines))

    # Record field provenance in raw_metadata for debugging.
    parser_meta = detail.raw_metadata.get("parser")
    if isinstance(parser_meta, dict):
        parser_meta["field_sources"] = field_sources


def _extract_fact_map(root: Tag, lines: list[str]) -> dict[str, str]:
    facts: dict[str, str] = {}

    # Semantically structured definition lists and tables.
    for dt in root.find_all("dt"):
        field_name = LABEL_TO_FIELD.get(normalize_space(dt.get_text(" ", strip=True)).rstrip(":").casefold())
        dd = dt.find_next_sibling("dd")
        if field_name and dd:
            _put_fact(facts, field_name, dd.get_text(" ", strip=True))
    for row in root.find_all("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if len(cells) >= 2:
            field_name = LABEL_TO_FIELD.get(normalize_space(cells[0].get_text(" ", strip=True)).rstrip(":").casefold())
            if field_name:
                _put_fact(facts, field_name, cells[1].get_text(" ", strip=True))

    # Label and value rendered as adjacent blocks, as seen in the authenticated
    # detail page supplied by the user.
    _SECTION_HEADINGS = {"description", "report project", "similar projects"}
    for index, line in enumerate(lines):
        field_name = LABEL_TO_FIELD.get(line.rstrip(":").casefold())
        if not field_name or field_name in facts:
            continue
        for candidate in lines[index + 1 : index + 4]:
            if _is_fact_label(candidate) or candidate.rstrip(":").casefold() in _SECTION_HEADINGS:
                break
            if _is_noise_line(candidate):
                continue
            if _is_prose_value(candidate):
                continue
            _put_fact(facts, field_name, candidate)
            break

    # Inline forms such as "Duration: 6 months" inside descriptions or legacy
    # layouts. Bound each value at the next known label.
    joined = " ".join(lines)
    all_aliases = [alias for aliases in FACT_LABELS.values() for alias in aliases]
    # Single-word aliases that commonly appear inside compound phrases and
    # cause over-matching when used for inline extraction.
    _SKIP_INLINE_ALIASES = {"contract", "start", "rate", "term", "workload", "hours"}
    for field_name, aliases in FACT_LABELS.items():
        if field_name in facts:
            continue
        for alias in aliases:
            if alias.casefold() in _SKIP_INLINE_ALIASES:
                continue
            # Build stop words: multi-word aliases are unconditional boundaries;
            # single-word aliases only work when followed by a colon.
            multi_stop = [
                value for value in all_aliases
                if value.casefold() != alias.casefold() and " " in value
            ]
            single_stop = [
                value for value in all_aliases
                if value.casefold() != alias.casefold()
                and " " not in value
                and value.casefold() not in _SKIP_INLINE_ALIASES
            ]
            multi_part = "|".join(re.escape(v) for v in sorted(multi_stop, key=len, reverse=True))
            single_part = "|".join(re.escape(v) for v in sorted(single_stop, key=len, reverse=True))
            # Multi-word labels are unconditional boundaries; single-word
            # labels only act as boundaries when followed by a colon.
            if multi_part and single_part:
                stop = f"(?:{multi_part})|(?:{single_part}\\s*:)"
            elif multi_part:
                stop = f"(?:{multi_part})"
            else:
                stop = f"(?:{single_part}\\s*:)"
            match = re.search(
                rf"\b{re.escape(alias)}\s*:?\s*(.+?)(?=\s+(?:{stop})\s)",
                joined,
                re.I,
            )
            if match:
                candidate = match.group(1)
                if _is_prose_value(candidate):
                    continue
                # For location, reject values that contain other field labels
                if field_name == "location":
                    _LEAKED = ("languages:", "type:", "duration:", "rate:", "workload:",
                               "workplace:", "contract:", "start date:", "budget:")
                    candidate_lower = candidate.lower()
                    if any(leak in candidate_lower for leak in _LEAKED):
                        continue
                _put_fact(facts, field_name, candidate)
                break

    return facts


def _put_fact(facts: dict[str, str], field_name: str, value: str) -> None:
    cleaned = normalize_space(value).strip("|•·- ")
    cleaned = _clean_fact_value(field_name, cleaned)
    if cleaned and cleaned.casefold() not in LABEL_TO_FIELD and len(cleaned) <= 500:
        facts.setdefault(field_name, cleaned)


def _clean_fact_value(field_name: str, value: str) -> str:
    if not value:
        return ""
    if field_name == "location":
        cleaned = normalize_space(value)
        cleaned = re.sub(r"^[,\s]+|[,\s]+$", "", cleaned)
        cleaned = re.sub(r"\s*,\s*", ", ", cleaned)
        return cleaned
    if field_name == "start_date":
        match = START_RE.search(value)
        return normalize_space(match.group(0)) if match else value
    if field_name == "duration":
        if DURATION_RE.search(value):
            split_val = re.split(
                r"\s+(?:Workload|Workplace|Rate|Contract type|Start date|Description|Responsibilities|Requirements):",
                value,
                flags=re.I,
            )[0]
            cleaned = normalize_space(split_val)
            cleaned = re.sub(
                r"\s+(?:\d{1,3}%\s*(?:remote|workload)?|full[- ]time|part[- ]time)$",
                "",
                cleaned,
                flags=re.I,
            )
            return cleaned
        return value
    if field_name == "workplace":
        percent = re.search(r"\b\d{1,3}%\s*remote\b", value, re.I)
        if percent:
            return normalize_space(percent.group(0))
        match = WORKPLACE_RE.search(value)
        return normalize_space(match.group(0)) if match else value
    if field_name == "contract_type":
        lowered = value.casefold()
        for contract in KNOWN_CONTRACT_TYPES:
            if contract.casefold() in lowered:
                return contract
        return value
    if field_name == "workload":
        if re.search(
            r"\b(?:full[- ]time|part[- ]time|\d+(?:\s*[-–]\s*\d+)?\s*hours?(?:\s+per\s+(?:week|month|day))?|\d+\s+days?\s+per\s+week|\d{1,3}%\s*workload|\d{1,3}%\s*allocation)\b",
            value,
            re.I,
        ):
            split_val = re.split(
                r"\s+(?:Duration|Workplace|Rate|Contract type|Start date|Description|Responsibilities|Requirements):",
                value,
                flags=re.I,
            )[0]
            return normalize_space(split_val)
        return value
    if field_name == "rate":
        return _extract_rate(value) or value
    return value


def _visible_lines(root: Tag) -> list[str]:
    lines: list[str] = []
    for value in root.find_all(string=True):
        if not isinstance(value, NavigableString):
            continue
        parent = value.parent
        if not isinstance(parent, Tag) or parent.name in {"script", "style", "noscript", "svg"}:
            continue
        if parent.find_parent(["script", "style", "noscript", "svg", "footer", "nav"]):
            continue
        text = normalize_space(unescape(str(value)))
        if text and (not lines or lines[-1] != text):
            lines.append(text)
    return lines


def _extract_description(soup: BeautifulSoup, lines: list[str], description_index: int, title: str = "") -> tuple[str, str]:
    selectors = [
        "#project-description",
        "[data-testid*='description']",
        "[class*='project-description']",
        "[class*='projectDescription']",
        "section.description",
    ]
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            text, html = _multiline_text(node), str(node)
            return _strip_title_prefix(text, title), html

    heading = soup.find(
        lambda tag: isinstance(tag, Tag)
        and tag.name in {"h2", "h3", "h4"}
        and normalize_space(tag.get_text(" ", strip=True)).casefold() == "description"
    )
    if heading:
        chunks: list[str] = []
        html_chunks: list[str] = []
        for sibling in heading.next_siblings:
            if isinstance(sibling, Tag):
                if sibling.name in {"h1", "h2", "footer"}:
                    break
                text = _multiline_text(sibling)
                if any(text.startswith(marker) for marker in FOOTER_MARKERS):
                    break
                if text:
                    chunks.append(text)
                    html_chunks.append(str(sibling))
        if chunks:
            combined = "\n".join(chunks)
            return _strip_title_prefix(combined, title), "\n".join(html_chunks)

    if description_index >= 0:
        desc_lines: list[str] = []
        for line in lines[description_index + 1 :]:
            if any(line.startswith(marker) for marker in FOOTER_MARKERS):
                break
            desc_lines.append(line)
        return _strip_title_prefix("\n".join(desc_lines).strip(), title), ""
    return "", ""


def _strip_title_prefix(text: str, title: str) -> str:
    """Remove a leading title repetition from description text."""
    if not text or not title:
        return text
    stripped = text.lstrip()
    title_fold = normalize_space(title).casefold()
    if stripped.casefold().startswith(title_fold):
        remainder = stripped[len(title):].lstrip("\n")
        return remainder if remainder else text
    return text


def _multiline_text(node: BeautifulSoup | Tag) -> str:
    # Render list items with "- " prefix for readability.
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, Tag) and child.name in {"ul", "ol"}:
            for li in child.find_all("li", recursive=False):
                text = normalize_space(unescape(li.get_text(" ", strip=True)))
                if text:
                    parts.append(f"- {text}")
        elif isinstance(child, Tag) and child.name == "li":
            text = normalize_space(unescape(child.get_text(" ", strip=True)))
            if text:
                parts.append(f"- {text}")
        elif isinstance(child, Tag):
            text = normalize_space(unescape(child.get_text("\n", strip=True)))
            if text:
                parts.append(text)
        elif isinstance(child, str):
            text = normalize_space(unescape(child))
            if text:
                parts.append(text)
    if parts:
        return "\n".join(parts)
    raw = node.get_text("\n", strip=True)
    lines = [normalize_space(unescape(value)) for value in raw.splitlines()]
    return "\n".join(value for value in lines if value)


def _extract_skill_links(root: BeautifulSoup | Tag, base_url: str) -> list[str]:
    skills: list[str] = []
    for anchor in root.find_all("a", href=True):
        href = urljoin(base_url + "/", anchor["href"])
        text = normalize_space(anchor.get_text(" ", strip=True))
        if not text or len(text) > 80:
            continue
        if re.search(r"/(?:skill|skills|project-skill|tag|technology)(?:/|-)", href, re.I):
            skills.append(text)
    return _unique_strings(skills)


def _find_apply_target(soup: BeautifulSoup) -> str:
    labels = {"apply now", "apply", "bewerben"}
    for node in soup.find_all(["a", "button", "input"]):
        text = normalize_space(
            node.get_text(" ", strip=True) if node.name != "input" else str(node.get("value") or "")
        ).casefold()
        if text not in labels:
            continue
        for attribute in ("href", "data-href", "data-url", "formaction"):
            value = normalize_space(str(node.get(attribute) or ""))
            if value:
                return value
        form = node.find_parent("form")
        if form and normalize_space(str(form.get("action") or "")):
            return normalize_space(str(form.get("action")))
        onclick = str(node.get("onclick") or "")
        match = re.search(r"(?:location(?:\.href)?|window\.open)\s*\(?\s*['\"]([^'\"]+)", onclick, re.I)
        if match:
            return match.group(1)
    return ""


def _has_apply_control(soup: BeautifulSoup) -> bool:
    for node in soup.find_all(["a", "button", "input"]):
        text = normalize_space(
            node.get_text(" ", strip=True) if node.name != "input" else str(node.get("value") or "")
        ).casefold()
        if text in {"apply now", "apply", "bewerben"}:
            return True
    return False


def _visible_company(main: Tag, title_node: Tag | None, lines: list[str], title: str) -> str:
    for selector in (
        "[data-testid*='company']",
        "[data-testid*='provider']",
        "[class*='company']",
        "[class*='provider']",
        "[class*='client']",
    ):
        node = main.select_one(selector)
        if node:
            value = normalize_space(node.get_text(" ", strip=True))
            if value and value != title and len(value) <= 200:
                return value

    if title_node:
        for sibling in title_node.find_all_next(limit=12):
            if not isinstance(sibling, Tag):
                continue
            if sibling.name in {"h2", "h3"} and normalize_space(sibling.get_text(" ", strip=True)).casefold() == "description":
                break
            if sibling.name == "a":
                value = normalize_space(sibling.get_text(" ", strip=True))
                href = normalize_space(str(sibling.get("href") or ""))
                if value and "/project/" not in href and value.casefold() not in {"apply now", "save to watchlist"}:
                    return value

    for value in lines[:8]:
        if value == title or _is_noise_line(value) or _is_metadata_line(value) or _is_fact_label(value):
            continue
        if _looks_like_fact_value(value) or len(value) > 160:
            continue
        return value
    return ""


def _company_link_after_title(title_node: Tag | None) -> Tag | None:
    if not title_node:
        return None
    for node in title_node.find_all_next("a", limit=8):
        text = normalize_space(node.get_text(" ", strip=True))
        href = normalize_space(str(node.get("href") or ""))
        if text and "/project/" not in href and text.casefold() not in {"apply now", "save to watchlist"}:
            return node
    return None


def _split_location(detail: ProjectDetail) -> None:
    if not detail.location or detail.location.casefold() in {"remote", "worldwide", "not specified"}:
        return
    cleaned = normalize_space(detail.location)
    cleaned = re.sub(r"^[,\s]+|[,\s]+$", "", cleaned)
    cleaned = re.sub(r"\s*,\s*", ", ", cleaned)
    detail.location = cleaned
    pieces = [normalize_space(value) for value in detail.location.split(",") if normalize_space(value)]
    if pieces and not detail.city and pieces[0].casefold() not in {"remote", "worldwide", "anywhere"}:
        detail.city = pieces[0]
    if len(pieces) > 1 and not detail.country:
        detail.country = pieces[-1]


def _extract_location_from_lines(lines: list[str]) -> str:
    for i, line in enumerate(lines):
        clean_line = normalize_space(line)
        if clean_line.casefold() in {"remote", "worldwide", "not specified"}:
            return clean_line
        if clean_line == "," and i > 0 and i + 1 < len(lines):
            prev_line = lines[i - 1].strip()
            next_line = lines[i + 1].strip()
            if not _looks_like_fact_value(prev_line) and not _looks_like_fact_value(next_line) and not _is_noise_line(prev_line) and not _is_noise_line(next_line):
                res = f"{prev_line}, {next_line}"
                res = re.sub(r"^[,\s]+|[,\s]+$", "", res)
                return re.sub(r"\s*,\s*", ", ", res)
        if clean_line.endswith(",") and i + 1 < len(lines):
            next_line = lines[i + 1].strip()
            if not _looks_like_fact_value(next_line) and not _is_noise_line(next_line) and not _is_report_noise(next_line):
                combined = normalize_space(f"{clean_line} {next_line}")
                combined = re.sub(r"^[,\s]+|[,\s]+$", "", combined)
                return re.sub(r"\s*,\s*", ", ", combined)
        if "," in clean_line and not _looks_like_fact_value(clean_line) and not clean_line.casefold().startswith("contact person") and not _is_metadata_line(clean_line):
            combined = re.sub(r"^[,\s]+|[,\s]+$", "", clean_line)
            combined = re.sub(r"\s*,\s*", ", ", combined)
            if combined:
                return combined
    return ""


def _first_workplace(lines: Iterable[str]) -> str:
    for line in lines:
        match = WORKPLACE_RE.search(line)
        if match:
            value = normalize_space(match.group(0))
            # Normalize known workplace values to title case for consistency.
            _WORKPLACE_NORM = {
                "remote": "Remote", "on-site": "On-site", "on site": "On-site",
                "hybrid": "Hybrid", "home-based": "Home-based", "home based": "Home-based",
                "fully remote": "Fully remote", "partial remote": "Partial remote",
            }
            return _WORKPLACE_NORM.get(value.lower(), value)
    return ""


def _first_contract_type(lines: Iterable[str]) -> str:
    for line in lines:
        lowered = line.casefold()
        for contract in KNOWN_CONTRACT_TYPES:
            if contract.casefold() in lowered:
                return contract
    return ""


def _first_duration(lines: Iterable[str]) -> str:
    for line in lines:
        match = DURATION_RE.search(line)
        if match:
            return normalize_space(match.group(0))
    return ""


def _first_start(lines: Iterable[str], duration: str = "") -> str:
    for line in lines:
        if line == duration:
            continue
        match = START_RE.search(line)
        if match:
            return normalize_space(match.group(0))
    return ""


def _looks_like_fact_value(text: str) -> bool:
    lowered = text.casefold()
    return bool(
        WORKPLACE_RE.search(text)
        or DURATION_RE.search(text)
        or START_RE.fullmatch(text)
        or any(value.casefold() in lowered for value in KNOWN_CONTRACT_TYPES)
        or lowered.startswith(("duration", "start date", "workload", "workplace", "location", "contract type"))
    )


def _is_fact_label(value: str) -> bool:
    return value.rstrip(":").casefold() in LABEL_TO_FIELD


def _is_metadata_line(value: str) -> bool:
    lowered = value.casefold()
    return bool(
        POSTED_RE.fullmatch(value)
        or PUBLISHED_ON_RE.search(value)
        or VIEWS_RE.fullmatch(value)
        or lowered in {"views", "posted"}
    )


def _is_noise_line(line: str) -> bool:
    stripped = normalize_space(line)
    lowered = stripped.casefold()
    return stripped in UI_NOISE or lowered in {value.casefold() for value in UI_NOISE} or _is_report_noise(stripped)


def _is_prose_value(text: str) -> bool:
    """Return True if text looks like a prose sentence rather than a fact value."""
    stripped = normalize_space(text)
    if not stripped:
        return True
    # Very long values are prose
    if len(stripped) > 120:
        return True
    # Ends with a period — almost certainly a sentence, not a fact value
    if stripped.endswith("."):
        return True
    # Starts with common prose words
    _PROSE_STARTERS = ("the ", "a ", "an ", "this ", "our ", "your ", "we ", "you ", "they ", "it ", "in ", "on ", "at ")
    if stripped.lower().startswith(_PROSE_STARTERS):
        return True
    # Single-word articles/propositions are not fact values
    if stripped.lower() in {"the", "a", "an", "this", "that", "in", "on", "at", "for", "with", "by"}:
        return True
    # Contains sentence punctuation typical of prose
    if stripped.count(".") > 1 or stripped.count(",") > 2:
        return True
    return False


def _is_report_noise(line: str) -> bool:
    lowered = line.casefold()
    return lowered.startswith("after submitting") or lowered.startswith("reason for reporting")


def _labeled_value(text: str, label: str, next_labels: tuple[str, ...]) -> str:
    cleaned_text = normalize_space(text)
    stop = "|".join(re.escape(value) for value in next_labels)
    match = re.search(rf"\b{re.escape(label)}\s*:?\s*(.+?)(?=\s+(?:{stop})\b|$)", cleaned_text, re.I)
    if not match:
        return ""
    val = normalize_space(match.group(1))
    val = re.sub(r"\s*[-–•·]\s*$", "", val)
    return val.strip()


def _extract_rate(text: str) -> str:
    RATE_LABEL = r"(?:Rate|Hourly rate|Daily rate|Budget|Pay rate)"
    CURRENCY = r"[€£$]\s?[\d,.]+"
    RANGE = r"(?:\s*[-–]\s*[€£$]?\s?[\d,.]+)?"
    UNIT = r"(?:\s*(?:per|/)?\s*(?:a?\s*(?:hour|day|hr|daily|hourly|month))?)"
    PAREN = r"(?:\s*\([^\n)]{1,50}\))?"
    QUALIFIER = r"(?:\s*\+\s*[^\n]+)?"
    NUMERIC_RATE = CURRENCY + RANGE + UNIT + PAREN + QUALIFIER
    patterns = [
        # "Rate: £21.83 per hour + holidays PAYE" or "Rate: £700/day (Inside IR35)"
        RATE_LABEL + r"\s*:?\s*(" + NUMERIC_RATE + r")",
        # "Day Rate: Up to £350 a day (Inside IR35)"
        r"(?:Day Rate)\s*:?\s*([Uu]p\s+to\s*" + CURRENCY + UNIT + PAREN + r")",
        # "Rate: Circa 150 Euro per hour"
        RATE_LABEL + r"\s*:?\s*([Cc]irca\s+[^\n]{2,60})",
        # "Rate: Open" or "Pay Rate: Competitive"
        RATE_LABEL + r"\s*:\s*([A-Za-z][\w-]{0,30})",
        # Standalone: "£674 p/d"
        r"(" + CURRENCY + r"\s*p/d)",
        # Standalone: "£350-£500/day"
        r"(" + CURRENCY + r"\s*[-–]\s*[€£$]?\s?[\d,.]+\s*(?:per|/)?\s*(?:day|hour|hr|month)" + PAREN + r")",
        # Standalone: "60-70€"
        r"([\d,.]+\s*[-–]\s*[\d,.]+\s*€)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            value = normalize_space(match.group(1))
            value = value.rstrip(",").strip()
            return value
    return ""


def _extract_posted_text(text: str) -> str:
    match = POSTED_RE.search(text)
    if match:
        return normalize_space(match.group(0))
    match = ABSOLUTE_DATETIME_RE.search(text)
    if match:
        return normalize_space(match.group(0))
    # Clock time (e.g. "21:03") — only standalone HH:MM, not part of a datetime.
    match = CLOCK_TIME_RE.search(text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            # Make sure it's not part of a datetime like "07/30/2026, 11:38 PM"
            start = match.start()
            if start == 0 or not text[start - 1].isdigit():
                return normalize_space(match.group(0))
    # European date (e.g. "29.07.2026")
    match = EUROPEAN_DATE_RE.search(text)
    if match:
        return normalize_space(match.group(0))
    return ""


def _extract_view_count(text: str) -> int | None:
    match = VIEWS_RE.search(text)
    if not match:
        return None
    try:
        return int(re.sub(r"[^0-9]", "", match.group(1)))
    except ValueError:
        return None


def _extract_absolute_date(text: str) -> str:
    patterns = (
        r"\b(?:0?[1-9]|[12]\d|3[01])[./-](?:0?[1-9]|1[0-2])[./-](?:20\d{2}|\d{2})\b",
        r"\b(?:0?[1-9]|[12]\d|3[01])\s+[A-Za-z]{3,9}\s+20\d{2}\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return normalize_space(match.group(0))
    return ""


def _normalize_scan_at(value: str | datetime | None) -> str:
    """Return one timezone-aware UTC ISO timestamp without microseconds."""

    return _iso_utc(_as_utc_datetime(value))


def _as_utc_datetime(value: str | datetime | None) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and normalize_space(value):
        candidate = normalize_space(value)
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            parsed = _parse_absolute_datetime(candidate, datetime.now(timezone.utc))
            if parsed is None:
                parsed = datetime.now(timezone.utc)
    else:
        try:
            parsed = datetime.fromisoformat(utc_now_iso())
        except ValueError:  # defensive only; utc_now_iso is controlled locally
            parsed = datetime.now(timezone.utc)

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _resolve_posted_at(value: str | None, scan_at: str | datetime | None) -> str:
    scanned = _as_utc_datetime(scan_at)
    text = normalize_space(value)
    if not text:
        return _iso_utc(scanned)

    relative = POSTED_RE.search(text)
    if relative:
        return parse_relative_posted_time(relative.group(0), scanned)

    absolute = _parse_absolute_datetime(text, scanned)
    return _iso_utc(absolute or scanned)


def _parse_absolute_datetime(text: str, scanned: datetime) -> datetime | None:
    """Parse common ISO and European posting-date formats.

    Naive timestamps are interpreted as UTC because the source page does not
    expose a reliable timezone alongside those values.  This is preferable to
    silently applying the computer's local timezone, which varies by machine.
    """

    value = normalize_space(text)
    if not value:
        return None

    lowered = value.casefold()
    # Standalone clock time (e.g. "23:55") — interpret as same day or yesterday
    # depending on whether the time is before or after the scan time.
    clock_match = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
    if clock_match:
        hour, minute = int(clock_match.group(1)), int(clock_match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            candidate = scanned.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= scanned:
                return candidate
            # Time is after scan — assume previous day.
            return candidate - timedelta(days=1)

    time_match = re.search(r"\b(?:at\s*)?(\d{1,2}):(\d{2})(?::(\d{2}))?\b", lowered)
    hour = int(time_match.group(1)) if time_match else 0
    minute = int(time_match.group(2)) if time_match else 0
    second = int(time_match.group(3) or 0) if time_match else 0

    if lowered.startswith("today"):
        return scanned.replace(hour=hour, minute=minute, second=second, microsecond=0)
    if lowered.startswith("yesterday"):
        previous = scanned - timedelta(days=1)
        return previous.replace(hour=hour, minute=minute, second=second, microsecond=0)

    # "Published on 07/30/2026, 11:38 PM" or "07/30/2026, 11:38 PM"
    pub_match = re.search(
        r"(?:Published\s+on\s+)?(\d{1,2})/(\d{1,2})/(\d{4}),?\s*(\d{1,2}):(\d{2})\s*(AM|PM|am|pm)",
        value,
    )
    if pub_match:
        month, day, year = int(pub_match.group(1)), int(pub_match.group(2)), int(pub_match.group(3))
        hour, minute = int(pub_match.group(4)), int(pub_match.group(5))
        ampm = pub_match.group(6).upper()
        if ampm == "PM" and hour != 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0
        try:
            return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
        except ValueError:
            pass

    candidates = [value]
    iso_match = re.search(
        r"\b\d{4}-\d{2}-\d{2}(?:[T\s]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?\b",
        value,
    )
    if iso_match and iso_match.group(0) not in candidates:
        candidates.append(iso_match.group(0))

    for candidate in candidates:
        normalized = candidate.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        # Python accepts offsets with a colon. Normalize compact +0000 forms.
        normalized = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", normalized)
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).replace(microsecond=0)

    formats = (
        "%m/%d/%Y %H:%M",
        "%m.%d.%Y %H:%M",
        "%m-%d-%Y %H:%M",
        "%m/%d/%Y",
        "%m.%d.%Y",
        "%m-%d-%Y",
        "%d/%m/%Y %H:%M",
        "%d.%m.%Y %H:%M",
        "%d-%m-%Y %H:%M",
        "%d/%m/%Y",
        "%d.%m.%Y",
        "%d-%m-%Y",
        "%d %B %Y %H:%M",
        "%d %b %Y %H:%M",
        "%d-%B-%Y %H:%M",
        "%d-%b-%Y %H:%M",
        "%d %B %Y",
        "%d %b %Y",
        "%d-%B-%Y",
        "%d-%b-%Y",
        "%B %d, %Y %H:%M",
        "%b %d, %Y %H:%M",
        "%B %d, %Y",
        "%b %d, %Y",
    )
    cleaned = re.sub(r"^(?:posted|published)(?:\s+on)?\s*:?-?\s*", "", value, flags=re.I)
    for fmt in formats:
        try:
            parsed = datetime.strptime(cleaned, fmt).replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            continue

    # Search for a date embedded inside a longer label.
    embedded_patterns = (
        (r"\b\d{1,2}[./-]\d{1,2}[./-]\d{4}(?:\s+\d{1,2}:\d{2})?\b", formats[:12]),
        (
            r"\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}(?:\s+\d{1,2}:\d{2})?\b",
            formats[12:20],
        ),
        (
            r"\b[A-Za-z]{3,9}\s+\d{1,2},\s+\d{4}(?:\s+\d{1,2}:\d{2})?\b",
            formats[20:],
        ),
    )
    for pattern, candidate_formats in embedded_patterns:
        match = re.search(pattern, value)
        if not match:
            continue
        for fmt in candidate_formats:
            try:
                return datetime.strptime(match.group(0), fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def _subtract_calendar_months(value: datetime, months: int) -> datetime:
    total_months = value.year * 12 + (value.month - 1) - months
    year, zero_based_month = divmod(total_months, 12)
    month = zero_based_month + 1
    day = min(value.day, monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _subtract_calendar_years(value: datetime, years: int) -> datetime:
    year = value.year - years
    day = min(value.day, monthrange(year, value.month)[1])
    return value.replace(year=year, day=day)


def _project_length_text(start_date: str, duration: str, workload: str) -> str:
    parts: list[str] = []
    for label, raw in (
        ("Start", start_date),
        ("Duration", duration),
        ("Workload", workload),
    ):
        value = normalize_space(raw)
        if value:
            parts.append(f"{label}: {value}")
    return " | ".join(parts)


def _merge_descriptions(card_text: str, detail_text: str) -> str:
    card = _clean_description_text(card_text)
    detail = _clean_description_text(detail_text)
    if not card:
        return detail
    if not detail:
        return card

    card_key = _comparison_text(card)
    detail_key = _comparison_text(detail)
    if card_key == detail_key:
        return detail if len(detail) >= len(card) else card
    if card_key and card_key in detail_key:
        return detail
    if detail_key and detail_key in card_key:
        return card
    # Merge but deduplicate shared lines.
    card_lines = [line for line in card.splitlines() if line.strip()]
    detail_lines = [line for line in detail.splitlines() if line.strip()]
    seen = set()
    merged_lines = []
    for line in card_lines + detail_lines:
        key = line.casefold().strip()
        if key in seen:
            continue
        seen.add(key)
        merged_lines.append(line)
    return "\n".join(merged_lines)


def _clean_description_text(value: str | None) -> str:
    lines = [normalize_space(line) for line in (value or "").splitlines()]
    output: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if not line or _is_noise_line(line) or _is_report_noise(line):
            continue
        key = line.casefold()
        if key in seen:
            continue
        seen.add(key)
        output.append(line)
    return "\n".join(output)


def _comparison_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _index_of(lines: list[str], value: str) -> int:
    target = normalize_space(value).casefold()
    return next((index for index, line in enumerate(lines) if line.casefold() == target), -1)


def _index_casefold(lines: list[str], value: str) -> int:
    target = value.casefold()
    return next((index for index, line in enumerate(lines) if line.casefold() == target), -1)


def _split_skills(value: Any) -> list[str]:
    if isinstance(value, list):
        return [normalize_space(_as_text(item)) for item in value if normalize_space(_as_text(item))]
    text = normalize_space(_as_text(value))
    if not text:
        return []
    return [normalize_space(item) for item in re.split(r"[,;|]", text) if normalize_space(item)]


def _salary_text(value: Any) -> str:
    if not isinstance(value, dict):
        return _as_text(value)
    currency = _as_text(value.get("currency"))
    inner = value.get("value")
    if isinstance(inner, dict):
        exact = _as_text(inner.get("value"))
        minimum = _as_text(inner.get("minValue"))
        maximum = _as_text(inner.get("maxValue"))
        unit = _as_text(inner.get("unitText"))
        amount = exact or minimum
        if maximum and maximum != minimum:
            amount = f"{minimum}-{maximum}" if minimum else maximum
        return " ".join(item for item in (currency, amount, unit) if item)
    return " ".join(item for item in (currency, _as_text(inner)) if item)


def _country_text(value: Any) -> str:
    if isinstance(value, dict):
        return _as_text(value.get("name"))
    return _as_text(value)


def _unique_strings(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(normalize_space(value) for value in values if normalize_space(value)))


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float)):
        return str(value)
    return ""