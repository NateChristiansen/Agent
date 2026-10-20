# ruff: noqa
from __future__ import annotations
from collections.abc import Sequence

TASKS_HINT = "<!-- A dependency graph"
HANDOFFS_VIEW = '```dataviewjs\nawait dv.view("harness/obsidian/views/handoffs")\n```\n'


def drop_tasks_hint(section: list[str]) -> list[str]:
    start = next((i for i, line in enumerate(section) if line.startswith(TASKS_HINT)), None)
    if start is None:
        return section
    end = next((i for i in range(start, len(section)) if section[i].rstrip().endswith("-->")), start)
    return section[:start] + section[end + 1 :]


def plan_body(number: str, title: str) -> str:
    return f'# {number} — {title}\n\n```dataviewjs\nawait dv.view("harness/obsidian/views/feature")\n```\n\n## Context\n\n## Decisions\n\n## Tasks\n- [ ]\n<!-- A dependency graph, not a sequence. Give each task what it needs and the paths it writes,\nlike "Shared record types (writes src/agent_cli/records/)" and "Dispatch consumer (needs #1;\nwrites src/agent_cli/dispatch/)". Everything whose needs have landed runs at once, in waves. -->\n\n## Open questions\n\n## Verification\n\n## Handoffs\n{HANDOFFS_VIEW}\n## Notes\n'


def log_header(number: str, slug: str) -> str:
    return f"# Log — {number} {slug}\n"


def phase_report_body(number: str, title: str, phase: str, tasks: Sequence[tuple[str, int]] = ()) -> str:
    rows = "".join((f"| {node} | #{task} | | | |\n" for node, task in tasks))
    return f"# {number} — {title}: phase {phase} report\n\n## Candidate head\n- integrated head:\n- phase base:\n- spec hash:\n\n## Task outcomes\n| Node | Task | Dispatch | Outcome | Commit |\n|---|---|---|---|---|\n{rows}\n## Review findings\n| Finding | Severity | Disposition |\n|---|---|---|\n\n## Verification\n- head:\n- config:\n- result:\n\n## Cost\n| Item | USD | Measured, estimated or unpriced |\n|---|---|---|\n\n## Time\n- started:\n- closed:\n- wall clock:\n"


def handoff_body(number: str, title: str, phase: str, next_phase: str) -> str:
    return f"# {number} — {title}: handoff from phase {phase} to phase {next_phase}\n\n## Feature\n\n## Phase {phase}, closed\n- report:\n- integrated head:\n\n## Open findings\n\n## Phase {next_phase} graph\n- graph:\n- spec:\n- spec hash:\n\n## Decisions\n"


def adr_body(number: str, title: str) -> str:
    return f"# ADR-{number}: {title}\n\n## Context\n\n## Decision\n\n## Consequences\n\n## Alternatives considered\n"
