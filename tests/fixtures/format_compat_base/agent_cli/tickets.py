# ruff: noqa
from __future__ import annotations
import re
from typing import Any
from .core import AgentError
from .records.narrow import as_list


def validate_tickets(tickets: list[str]) -> list[str]:
    out: list[str] = []
    for t in tickets:
        t = str(t).strip()
        if not t or any((c.isspace() for c in t)):
            raise AgentError(f"ticket id {t!r} must be non-empty with no spaces (e.g. TK#123, JIRA-45)")
        if t not in out:
            out.append(t)
    return out


def tickets_of(meta: dict[str, Any]) -> list[str]:
    return [str(t) for t in as_list(meta.get("tickets"))]


def ticket_slug(ticket: str) -> str:
    return re.sub("[^a-z0-9-]", "", ticket.lower())
