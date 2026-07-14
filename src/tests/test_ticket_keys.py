"""Offline tests for Jira ticket key parsing."""

from __future__ import annotations

import pytest

from crewai_headless_flow.ticket_keys import parse_jira_ticket_key

pytestmark = pytest.mark.offline


@pytest.mark.parametrize(
    ("request_text", "expected"),
    [
        ("AS-5245", "AS-5245"),
        ("as-5245", "AS-5245"),
        ("Please implement AS-1002 for payroll", "AS-1002"),
        (
            "https://example.atlassian.net/browse/AS-5245",
            "AS-5245",
        ),
        (
            "https://example.atlassian.net/jira/software/projects/AS/boards/1?selectedIssue=AS-5245",
            "AS-5245",
        ),
        ("", None),
        ("add auth helpers", None),
        ("APTM-8424", None),
    ],
)
def test_parse_jira_ticket_key(request_text: str, expected: str | None) -> None:
    assert parse_jira_ticket_key(request_text) == expected


@pytest.mark.parametrize(
    ("request_text", "expected"),
    [
        ("AS-5245", "AS-5245"),
        ("as-5245", "AS-5245"),
        (
            "https://example.atlassian.net/browse/AS-5245",
            "AS-5245",
        ),
        (
            "https://example.atlassian.net/jira/software/projects/AS/boards/1?selectedIssue=AS-5245",
            "AS-5245",
        ),
        ("Please implement AS-1002 for payroll", None),
        ("", None),
        ("add auth helpers", None),
    ],
)
def test_parse_jira_ticket_key_strict(request_text: str, expected: str | None) -> None:
    assert parse_jira_ticket_key(request_text, strict=True) == expected
