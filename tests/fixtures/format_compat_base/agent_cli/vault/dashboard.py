# ruff: noqa
from __future__ import annotations
from pathlib import Path

VIEWS_DIR = "harness/obsidian/views"


def view_block(name: str, args: str | None = None) -> str:
    call = f'''await dv.view("{VIEWS_DIR}/{name}"{(", " + args if args else "")})'''
    return f"```dataviewjs\n{call}\n```"


def render_dashboard(root: Path) -> str:
    return f"---\ntags:\n  - dashboard\ncssclasses:\n  - agent-dashboard\ngenerated: true\ngenerated_by: agent status render\n---\n{view_block('nav')}\n\n```agent-live\n```\n\n## Journal\n\n{view_block('journal', '{ days: 7 }')}\n\n## Recent log\n\n{view_block('log', '{ hours: 24, limit: 20 }')}\n"


def render_index(root: Path) -> str:
    return f"""---\ntags:\n  - index\ncssclasses:\n  - agent-dashboard\ngenerated: true\ngenerated_by: agent status render\n---\n[[Dashboard|dashboard]] · [[STATUS|STATUS.md]] · [[harness/README|harness]] · [[adrs/README|ADR index]]\n\n## Open features\n\n{view_block("features")}\n\n## Closed features\n\n{view_block("index", '{ section: "closed" }')}\n\n## Decisions\n\n{view_block("index", '{ section: "adrs" }')}\n\n## Docs\n\n### Retros\n\n{view_block("index", '{ section: "docs", kind: "retro" }')}\n\n### Design\n\n{view_block("index", '{ section: "docs", kind: "design" }')}\n\n### Audits\n\n{view_block("index", '{ section: "docs", kind: "audit" }')}\n\n### QA plans\n\n{view_block("index", '{ section: "docs", kind: "qa-plan" }')}\n\n## Handoffs\n\n{view_block("index", '{ section: "handoffs" }')}\n\n## Journal\n\n{view_block("journal", "{ days: 30 }")}\n"""
