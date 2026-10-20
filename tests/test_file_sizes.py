"""The always-loaded instructions fit Codex's byte budget, and AGENTS.md fits its own ceiling.

The 500-line cap on Python files that lived here was dropped: modules
were split only to fit it, and the cap forced detours on ordinary fixes.
"""

from __future__ import annotations

from pathlib import Path

# The numbers live beside the pre-commit gate that enforces them on staged content (ADR-0026).
from agent_cli.gitgate import INSTRUCTION_BUDGET, PROJECT_DOC_MAX_BYTES

ROOT = Path(__file__).resolve().parents[1]

# Codex stops adding instruction files once their COMBINED size reaches
# `project_doc_max_bytes`, 32 KiB by default, and says nothing when it does: whatever falls
# past the line is dropped in file order, so the tail of AGENTS.md disappears with no error
# and nothing in the transcript. Claude Code's own limit is higher (40k), so Codex binds.
#
# The gate is at 95% of the real cliff rather than some rounder, stricter number. A lower bar
# would be an invented constraint, and this feature is about not inventing those (ADR-0024).
# The count is of every instruction file Codex would walk, not just the root one, because the
# limit is on their total — a gate weighing one file passes while a second pushes it over.
# What Codex actually walks: AGENTS.override.md and every AGENTS.md from the root down.
# `.claude/rules/` is deliberately absent — Claude loads it alongside AGENTS.md, but Codex
# never reads it, so it does not spend this budget.
INSTRUCTION_GLOBS = ("AGENTS.override.md", "AGENTS.md", "*/AGENTS.md", "**/AGENTS.md")


def _instruction_files(root: Path = ROOT) -> dict[str, int]:
    """Every instruction file Codex would load, with its size, deduplicated across globs.

    `.claude/worktrees/` is skipped: each entry there is a separate checkout of this repo (a
    subagent worktree) with its own AGENTS.md, and counting four of them failed this gate at
    42,830 bytes on 2026-10-03."""
    found: dict[str, int] = {}
    for pattern in INSTRUCTION_GLOBS:
        for path in root.glob(pattern):
            relative = path.relative_to(root)
            if relative.parts[:2] == (".claude", "worktrees"):
                continue
            if path.is_file() and not {".agent", ".git", "vendor"}.intersection(relative.parts):
                found[str(relative)] = path.stat().st_size
    return dict(sorted(found.items()))


# AGENTS.md's own ceiling, well under the Codex cliff above. Every session reads this file before
# its first word, and each added rule dilutes the rest. Anthropic's costs page keeps CLAUDE.md
# "under 200 lines" (code.claude.com/docs/en/costs, fetched 2026-10-12), and its context-
# engineering post treats context as a finite budget best spent just in time (published
# 2025-09-29). The numbers are the file's size when they were
# set (160 lines, 12,272 bytes) plus a small byte margin and no line margin: new guidance
# pays for itself by moving or deleting lines, or a mechanism replaces the prose.
AGENTS_MAX_LINES = 160
AGENTS_MAX_BYTES = 12_500


def test_agents_md_stays_inside_its_own_budget() -> None:
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    lines, size = len(text.splitlines()), len(text.encode("utf-8"))
    assert lines <= AGENTS_MAX_LINES and size <= AGENTS_MAX_BYTES, (
        f"AGENTS.md is {lines} lines and {size:,} bytes; the ceiling is {AGENTS_MAX_LINES} lines and "
        f"{AGENTS_MAX_BYTES:,} bytes. Move the detail to the skill or harness doc read at that point, "
        "or delete a rule a mechanism now holds."
    )


def test_only_subagent_worktrees_are_left_out_of_the_instruction_count(tmp_path):
    for rel in ("AGENTS.md", "docs/AGENTS.md", ".claude/AGENTS.md", ".claude/worktrees/agent-x/AGENTS.md"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x")
    found = {Path(p).as_posix() for p in _instruction_files(tmp_path)}
    assert found == {"AGENTS.md", "docs/AGENTS.md", ".claude/AGENTS.md"}


def test_always_loaded_instructions_fit_in_the_budget() -> None:
    """The always-loaded budget is bytes, not rules, and overflowing it fails silently.

    Nobody had this number until 2026-09-23, when inlining the standing rules took AGENTS.md
    from 21,504 to 27,044 bytes — 26% larger, spending nearly half the remaining headroom, in
    one unmeasured edit. The size is printed on every run so the next person sees the cost
    before making it rather than after.

    This gate covers this repo's contribution, which is the part we control. A reader's own
    global instruction file, or a lowered `project_doc_max_bytes`, spends the same budget and
    is invisible from here. That is not checked: it is their config, and a check reporting
    something nobody can act on is noise. So passing means "we are not the ones overflowing
    it", not "no truncation will happen".
    """
    sizes = _instruction_files()
    total = sum(sizes.values())
    percent = 100 * total / PROJECT_DOC_MAX_BYTES
    print(f"\ninstructions: {total:,} B, {percent:.1f}% of {PROJECT_DOC_MAX_BYTES:,} B — {sizes}")
    assert total <= INSTRUCTION_BUDGET, (
        f"always-loaded instructions are {total:,} bytes, {percent:.1f}% of Codex's "
        f"{PROJECT_DOC_MAX_BYTES:,}-byte project_doc_max_bytes (budget {INSTRUCTION_BUDGET:,}). "
        f"Codex stops adding files at the cap and says nothing, so the tail is dropped in file "
        f"order. Shorten one of these or move it to a skill: {sizes}"
    )
