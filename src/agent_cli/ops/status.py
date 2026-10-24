"""Status ops (render/check) and obsidian ops."""

from __future__ import annotations

from pathlib import Path

from .. import core
from .. import obsidian as obsidian_mod
from .. import status as status_mod
from ..core import EXIT_NOT_FOUND, EXIT_OK
from ._shared import Op

# ---------------------------------------------------------------- status


def status_render(root: Path) -> Op:
    payload = status_mod.render(root)
    return Op(
        payload,
        f"rendered Dashboard.md, Index.md, bases/, adrs/README.md, {core.agent_display('status.json')} (hash {payload['source_hash']})",
    )


def status_check(root: Path) -> Op:
    """Are the generated files current?

    Exits non-zero when they are stale, which is what CI and the pre-commit reflex read."""
    result = status_mod.check(root)
    stale = [
        f"{name} (expected {row['expected']}, found {row['found']})"
        for name, row in result["views"].items()
        if not row["fresh"]
    ]
    text = "fresh" if result["fresh"] else f"stale: {'; '.join(stale) or result['reason']}"
    return Op(result, text, code=EXIT_OK if result["fresh"] else EXIT_NOT_FOUND)


# ---------------------------------------------------------------- obsidian


def obsidian_setup(root: Path) -> Op:
    result = obsidian_mod.setup(root)
    lines = ["wrote:"] + [f"  {w}" for w in result["written"]]
    if result["missing_plugins"]:
        lines.append("install these in Settings → Community plugins, then restart Obsidian:")
        lines += [f"  {m}" for m in result["missing_plugins"]]
    lines.append("optional:")
    lines += [f"  {m}" for m in result["optional_plugins"]]
    lines.append(
        "obsidian CLI: "
        + (
            "available"
            if result["cli_available"]
            else "not on PATH (Settings → General → Command line interface)"
        )
    )
    return Op(result, "\n".join(lines))


def obsidian_open(root: Path, *, path: str) -> Op:
    result = obsidian_mod.open_note(root, path)
    return Op(result, result.get("skipped") or f"opened {path}")


def obsidian_daily(root: Path) -> Op:
    result = obsidian_mod.daily()
    return Op(result, result.get("skipped") or "opened today's daily note")


def obsidian_search(root: Path, *, query: str) -> Op:
    result = obsidian_mod.search(query)
    return Op(result, result.get("skipped") or (result.get("stdout") or "(no results)"))
