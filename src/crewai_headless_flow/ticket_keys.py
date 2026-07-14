"""Parse Jira-style ticket keys from free-text requests / URLs."""

from __future__ import annotations

import re

# Project key AS-#### (Asure v1). Also accepts browse/selectedIssue URLs.
_BARE_TICKET_RE = re.compile(r"^AS-\d+$", re.IGNORECASE)
_TICKET_KEY_RE = re.compile(r"\b(AS-\d+)\b", re.IGNORECASE)
_JIRA_URL_RE = re.compile(
    r"(?:selectedIssue=|browse/)(AS-\d+)\b",
    re.IGNORECASE,
)
# Matches bare keys with or without hyphen (AS-5245 or AS5245).
_NORMALIZABLE_KEY_RE = re.compile(r"^(AS)-?(\d+)$", re.IGNORECASE)


def parse_jira_ticket_key(request: str, *, strict: bool = False) -> str | None:
    """Return a normalized ``AS-####`` key from a request string, or None.

    Accepts a bare key (``AS-5245``), a Jira browse / selectedIssue URL, or
    (when ``strict`` is False) free text containing a key. Always returns
    uppercase ``AS-`` prefix.
    """

    text = (request or "").strip()
    if not text:
        return None

    url_match = _JIRA_URL_RE.search(text)
    if url_match:
        return url_match.group(1).upper()

    if strict:
        if _BARE_TICKET_RE.fullmatch(text):
            return text.upper()
        return None

    key_match = _TICKET_KEY_RE.search(text)
    if key_match:
        return key_match.group(1).upper()

    return None


def normalize_jira_key(raw: str) -> str:
    """Return a canonical ``AS-####`` key, raising ValueError if unrecognisable.

    Accepts ``AS-5245``, ``as-5245``, ``AS5245`` (no hyphen), or any variant
    with surrounding whitespace. Raises ValueError for anything else.
    """
    text = (raw or "").strip()
    m = _NORMALIZABLE_KEY_RE.fullmatch(text)
    if not m:
        raise ValueError(f"Not a valid Jira key: {raw!r}")
    return f"{m.group(1).upper()}-{m.group(2)}"
