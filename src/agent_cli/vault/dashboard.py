"""The two vault home notes. Dashboard.md carries current tasks: what's in flight, what's
waiting on an answer. Index.md carries everything else: decisions, docs, handoff history, the
journal archive."""

from __future__ import annotations

from pathlib import Path

VIEWS_DIR = "harness/obsidian/views"


def view_block(name: str, args: str | None = None) -> str:
    """A dataviewjs block that runs one shared view script from `harness/obsidian/views/`."""
    call = f'await dv.view("{VIEWS_DIR}/{name}"{", " + args if args else ""})'
    return f"```dataviewjs\n{call}\n```"


def render_dashboard(root: Path) -> str:
    """Current tasks only: what needs the owner, who is running, and what's in flight. Everything
    else (decisions, docs, handoff history, the journal archive) lives in Index.md.

    The note holds no data. The live plugin pane reads `.agent/live.json`;
    The pane also reads `.agent/status.json` for features and tasks; DataviewJS renders
    the journal, log and navigation. The note is
    written only when this layout changes, so it no longer goes stale or churns the notes repo."""
    return f"""\
---
tags:
  - dashboard
cssclasses:
  - agent-dashboard
generated: true
generated_by: agent status render
---
{view_block("nav")}

```agent-live
```

## Journal

{view_block("journal", "{ days: 7 }")}

## Recent log

{view_block("log", "{ hours: 24, limit: 20 }")}
"""


def render_index(root: Path) -> str:
    """Everything the dashboard does not carry: decisions, docs, handoff history, the full
    journal. One stop for anything not a current task."""
    return f"""\
---
tags:
  - index
cssclasses:
  - agent-dashboard
generated: true
generated_by: agent status render
---
[[Dashboard|dashboard]] · [[harness/README|harness]] · [[adrs/README|ADR index]]

## Open features

{view_block("features")}

## Closed features

{view_block("index", '{ section: "closed" }')}

## Requests

{view_block("index", '{ section: "requests" }')}

## Resolved requests

{view_block("index", '{ section: "resolved-requests" }')}

## Decisions

{view_block("index", '{ section: "adrs" }')}

## Docs

### Retros

{view_block("index", '{ section: "docs", kind: "retro" }')}

### Design

{view_block("index", '{ section: "docs", kind: "design" }')}

### Audits

{view_block("index", '{ section: "docs", kind: "audit" }')}

### QA plans

{view_block("index", '{ section: "docs", kind: "qa-plan" }')}

## Handoffs

{view_block("index", '{ section: "handoffs" }')}

## Journal

{view_block("journal", "{ days: 30 }")}
"""
