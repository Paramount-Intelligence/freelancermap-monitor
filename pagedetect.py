"""Shared page-classification semantics.

Single source of truth for error-page, generic-title and challenge
detection used by ``browser.py``, ``monitor.py`` and ``parser.py`` so
the three modules can never drift apart again.

Detection rules (reliability mandate):

* Error pages are detected only from *context*: an anchored status
  heading (``404 Not Found``, ``500 Internal Server Error``, ``Error
  500``, ``Page 404 - Not Found``) or a written phrase such as "not
  found", "access denied" or "internal server error".  A bare number
  like ``404`` or ``500`` is never treated as an error -- real project
  titles such as "Error Handling Engineer", "Fortune 500 Data
  Architect" or "Login Security Specialist" must survive unchanged.
* Generic titles (``404 Not Found``, ``Error``, ``Login``, ...) are
  compared exactly against a small set after normalisation.  Substring
  matching is banned for the same reason.
* A challenge (CAPTCHA / bot check / MFA) is only reported when there
  is explicit evidence -- two explicit verification phrases, one
  explicit phrase corroborated by a challenge marker, a visible
  challenge iframe, or a one-time-code form.  A lone "please wait" or
  a bare recaptcha ``<script>`` tag is never sufficient.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse


LOGIN_PATH_RE = re.compile(r"/(?:login|sign-in)(?:/|$)", re.I)


def is_login_url(url: str) -> bool:
    """Return True if the URL path indicates a login or sign-in page."""
    try:
        parsed = urlparse(url)
        return bool(LOGIN_PATH_RE.search(parsed.path))
    except Exception:
        return False


def normalize_generic_title(title: str) -> str:
    """Collapse whitespace and casefold a title for exact comparison."""
    return re.sub(r"\s+", " ", title.casefold()).strip()


# Exact-match generic titles.  Only a title that is *identical* after
# normalisation is rejected; substrings are never matched.
GENERIC_ERROR_TITLES: frozenset[str] = frozenset(
    {
        "404",
        "404 not found",
        "410 gone",
        "429 too many requests",
        "500 internal server error",
        "502 bad gateway",
        "503 service unavailable",
        "access denied",
        "attention required",
        "bad gateway",
        "checking your browser",
        "error",
        "find the perfect project",
        "forbidden",
        "freelance jobs",
        "freelance jobs & it projects worldwide",
        "freelance jobs & it projects worldwide | freelancermap",
        "internal server error",
        "it projects",
        "it projects & freelance jobs",
        "just a moment",
        "log in",
        "login",
        "not found",
        "page does not exist",
        "page not found",
        "resource is gone",
        "server error",
        "service unavailable",
        "sign in",
        "something went wrong",
        "temporarily unavailable",
        "too many requests",
        "verify you are human",
    }
)


# Contextual error pattern.  Status codes are only meaningful when
# followed by an error word (``404 Not Found``, ``410 Resource is
# Gone``, ``500 Internal Server Error``) or when preceded by the words
# "error"/"status" (``Error 500``).  A bare ``\b404\b`` never matches.
_CONTEXTUAL_ERROR_RE = re.compile(
    r"(?:"
    r"(?:^|[^\w])(?:404|410|429|500|502|503)"
    r"(?:\s*[-–—]\s*|\s+)"
    r"(?:not\s+found|gone|too\s+many\s+requests|internal\s+server\s+error|"
    r"server\s+error|bad\s+gateway|service\s+unavailable|"
    r"resource\s+is\s+gone|page\s+does\s+not\s+exist)\b"
    r"|\b(?:error|status)\s+(?:404|410|429|500|502|503)\b"
    r"|\bpage\s+not\s+found\b"
    r"|\bpage\s+does\s+not\s+exist\b"
    r"|\bresource\s+is\s+gone\b"
    r"|\binternal\s+server\s+error\b"
    r"|\bservice\s+unavailable\b"
    r"|\bbad\s+gateway\b"
    r"|\btoo\s+many\s+requests\b"
    r"|\btemporarily\s+unavailable\b"
    r"|\baccess\s+denied\b"
    r"|\bsomething\s+went\s+wrong\b"
    r"|\btry\s+again\s+later\b"
    r"|\ban?\s+error\s+occurred\b"
    r")",
    re.I,
)

# Backwards-compatible names kept for existing imports; both share the
# same contextual semantics.
ERROR_TITLE_RE = _CONTEXTUAL_ERROR_RE
ERROR_BODY_RE = _CONTEXTUAL_ERROR_RE


def is_generic_error_title(title: str) -> bool:
    """True when the whole title is a known generic/error/login title."""
    return bool(title) and normalize_generic_title(title) in GENERIC_ERROR_TITLES


def has_error_context(text: str) -> bool:
    """True when the text contains contextual error-page evidence."""
    return bool(text) and bool(_CONTEXTUAL_ERROR_RE.search(text))


def has_error_title(title: str) -> bool:
    """True when a title is generic or contains contextual error evidence."""
    return is_generic_error_title(title) or has_error_context(title)


def detect_error(title: str, body_text: str) -> bool:
    """Classify a title/body pair as an error page."""
    return has_error_title(title) or has_error_context(body_text)


# --- Challenge (CAPTCHA / bot check / MFA) detection ----------------------

# Explicit, unambiguous challenge phrases.
_CHALLENGE_EXPLICIT_RE = re.compile(
    r"\b(?:just\s+a\s+moment|attention\s+required|verify\s+you\s+are\s+human|"
    r"complete\s+the\s+captcha|solve\s+the\s+captcha|prove\s+you\s+are\s+human)\b",
    re.I,
)

# Corroborating markers: never sufficient alone, but they back up an
# explicit phrase or a visible challenge widget.
_CHALLENGE_MARKER_RE = re.compile(
    r"\b(?:checking\s+your\s+browser|enable\s+javascript|security\s+check|"
    r"ray\s+id|cf-chl|challenge-platform|an?\s+automated|bot\s+check|"
    r"one\s+more\s+step|verify\s+your\s+identity|captcha|recaptcha|hcaptcha|"
    r"turnstile|please\s+wait)\b",
    re.I,
)

# A captcha *widget* iframe (reCAPTCHA / hCaptcha / Turnstile) is dual-use: it
# is embedded in ordinary forms (apply / contact / login) on fully-rendered
# pages AND used as the payload of an interstitial. It is therefore never
# definitive evidence of a full-page challenge on its own.
_WIDGET_IFRAME_RE = re.compile(
    r"<iframe\b[^>]*\b(?:src|data-src)\s*=\s*[\"'][^\"']*"
    r"(?:recaptcha|hcaptcha|turnstile)[^\"']*[\"']",
    re.I,
)

# An interstitial-platform iframe (Cloudflare challenge-platform / cf-chl /
# Arkose) is a full-page bot check by nature, so it remains definitive.
_INTERSTITIAL_IFRAME_RE = re.compile(
    r"<iframe\b[^>]*\b(?:src|data-src)\s*=\s*[\"'][^\"']*"
    r"(?:challenge-platform|cf-chl|arkose)[^\"']*[\"']",
    re.I,
)

# Markers that specifically indicate an anti-bot interstitial, as opposed to a
# captcha widget embedded in a form. The generic captcha/recaptcha/hcaptcha/
# turnstile terms are deliberately excluded: a widget iframe already proves a
# captcha is present, so those words must not also count as corroboration on a
# content-rich page (e.g. a security project whose description says "captcha").
_INTERSTITIAL_MARKER_RE = re.compile(
    r"\b(?:checking\s+your\s+browser|enable\s+javascript|security\s+check|"
    r"ray\s+id|cf-chl|challenge-platform|an?\s+automated|bot\s+check|"
    r"one\s+more\s+step|verify\s+your\s+identity|please\s+wait)\b",
    re.I,
)

# A page whose visible body carries less than this much text is treated as
# content-thin: a real project detail page renders thousands of characters,
# while a bare interstitial is essentially empty apart from the widget.
_MIN_REAL_BODY_CHARS = 200

# Backwards-compatible broad pattern (kept for external/diagnostic callers).
CHALLENGE_IFRAME_RE = re.compile(
    r"<iframe\b[^>]*\b(?:src|data-src)\s*=\s*[\"'][^\"']*"
    r"(?:recaptcha|hcaptcha|turnstile|challenge-platform|cf-chl|arkose)[^\"']*[\"']",
    re.I,
)

# A recaptcha/hCaptcha/Turnstile script tag alone is NOT a challenge;
# only the interactive widget counts.
CHALLENGE_SCRIPT_ONLY_RE = re.compile(
    r"<script\b[^>]*\bsrc\s*=\s*[\"'][^\"']*https?://[^\"']*"
    r"(?:recaptcha|hcaptcha|turnstile|challenge-platform|cf-chl)[^\"']*[\"']",
    re.I,
)

# A one-time-code (MFA / OTP) input is a verification form.
OTP_FORM_RE = re.compile(
    r"<input\b[^>]*\bautocomplete\s*=\s*[\"']one-time-code[\"']",
    re.I,
)


def detect_challenge(
    title: str,
    body_text: str,
    page_source: str = "",
) -> bool:
    """Classify a title/body/source triple as a challenge page.

    Rules:
    * two explicit verification phrases (e.g. title "Just a moment"
      plus body "Verify you are human") corroborate each other;
    * one explicit phrase needs at least one corroborating marker;
    * a Cloudflare/Arkose interstitial iframe or a one-time-code (MFA)
      form is definitive;
    * a captcha *widget* iframe (reCAPTCHA / hCaptcha / Turnstile) is only
      a challenge when the rest of the page corroborates it -- challenge
      language in the title/body, an interstitial marker, or no real body
      content. An embedded form widget on an otherwise content-rich page
      (e.g. a project detail with an apply-form captcha) must never be
      mistaken for a full-page bot block.
    """
    title = title or ""
    body = body_text or ""
    source = page_source or ""

    title_explicit = bool(_CHALLENGE_EXPLICIT_RE.search(title))
    body_explicit = bool(_CHALLENGE_EXPLICIT_RE.search(body))
    explicit_count = int(title_explicit) + int(body_explicit)
    if explicit_count >= 2:
        return True
    if explicit_count == 1 and (
        _CHALLENGE_MARKER_RE.search(title) or _CHALLENGE_MARKER_RE.search(body)
    ):
        return True

    # A full anti-bot interstitial platform iframe, or an MFA one-time-code
    # form, is definitive evidence of a verification gate.
    if _INTERSTITIAL_IFRAME_RE.search(source) or OTP_FORM_RE.search(source):
        return True

    # A captcha *widget* iframe is only a challenge when the page otherwise
    # looks like an interstitial: challenge language, an interstitial marker,
    # or a content-thin body. On a content-rich page it is a form widget.
    if _WIDGET_IFRAME_RE.search(source):
        thin_body = len(body.strip()) < _MIN_REAL_BODY_CHARS
        if (
            explicit_count >= 1
            or _INTERSTITIAL_MARKER_RE.search(title)
            or _INTERSTITIAL_MARKER_RE.search(body)
            or thin_body
        ):
            return True
    return False
