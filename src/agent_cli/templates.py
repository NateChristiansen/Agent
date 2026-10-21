"""Skeleton bodies for generated markdown files."""

from __future__ import annotations

TASKS_HINT = "<!-- A dependency graph"  # the template's Tasks hint; the first real task replaces it
HANDOFFS_VIEW = '```dataviewjs\nawait dv.view("harness/obsidian/views/handoffs")\n```\n'


def drop_tasks_hint(section: list[str]) -> list[str]:
    """A Tasks section's lines without the template's graph hint, which real tasks replace."""
    start = next((i for i, line in enumerate(section) if line.startswith(TASKS_HINT)), None)
    if start is None:
        return section
    end = next((i for i in range(start, len(section)) if section[i].rstrip().endswith("-->")), start)
    return section[:start] + section[end + 1 :]


def plan_body(number: str, title: str) -> str:
    return f"""# {number} — {title}

```dataviewjs
await dv.view("harness/obsidian/views/feature")
```

## Context

## Decisions

## Tasks
- [ ]
<!-- A dependency graph, not a sequence. Give each task what it needs and the paths it writes,
like "Shared record types (writes src/agent_cli/records/)" and "Dispatch consumer (needs #1;
writes src/agent_cli/dispatch/)". Everything whose needs have landed runs at once, in waves. -->

## Open questions

## Verification

## Handoffs
{HANDOFFS_VIEW}
## Notes
"""


REQUEST_RESULT = "Result"
REQUEST_ROUNDS = "Review rounds"
# The feature plan sections a request lacks; `requests.promote` adds them in this order.
FEATURE_ONLY_SECTIONS = ("Decisions", "Open questions", "Verification")


def request_body(number: str, title: str, category: str, ask: str) -> str:
    """A request's plan: the ask is the Context a worker reads, Tasks is optional and starts empty,
    Result holds what was delivered. Tasks and Handoffs stay canonical for the parsers and the
    document protection that read every plan."""
    return f"""# Request {number} — {title}

```dataviewjs
await dv.view("harness/obsidian/views/feature")
```

## Context

Ask ({category}): {ask.strip()}

## Tasks

## {REQUEST_RESULT}

## {REQUEST_ROUNDS}

## Handoffs
{HANDOFFS_VIEW}
## Notes
"""


def log_header(number: str, slug: str) -> str:
    return f"# Log — {number} {slug}\n"


def adr_body(number: str, title: str) -> str:
    return f"""# ADR-{number}: {title}

## Context

## Decision

## Consequences

## Alternatives considered
"""
