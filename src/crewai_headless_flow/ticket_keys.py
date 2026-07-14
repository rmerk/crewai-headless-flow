"""Parse Jira-style ticket keys from free-text requests / URLs."""

from __future__ import annotations

import re

# Project key AS-#### (Asure v1). Also accepts browse/selectedIssue URLs.
_TICKET_KEY_RE = re.compile(r"\b(AS-\d+)\b", re.IGNORECASE)
_JIRA_URL_RE = re.compile(
    r"(?:selectedIssue=|browse/)(AS-\d+)\b",
    re.IGNORECASE,
)


def parse_jira_ticket_key(request: str) -> str | None:
    """Return a normalized ``AS-####`` key from a request string, or None.

    Accepts a bare key (``AS-5245``), a sentence containing one, or a Jira
    browse / selectedIssue URL. Always returns uppercase ``AS-`` prefix.
    """

    text = (request or "").strip()
    if not text:
        return None

    url_match = _JIRA_URL_RE.search(text)
    if url_match:
        return url_match.group(1).upper()

    key_match = _TICKET_KEY_RE.search(text)
    if key_match:
        return key_match.group(1).upper()

    return None
