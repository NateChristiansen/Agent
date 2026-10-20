"""Manager settings that keep durable work inside the vault."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_plan_drafts_and_memory_stay_out_of_the_user_directory() -> None:
    """Plan drafts landed under ~/.claude hours after the vault rule was written.

    Claude Code had kept its default plans directory, so writing the rule did not change the
    behavior. The manager settings must route drafts to the vault scratch area and disable
    automatic memory outside the vault.
    """
    settings = json.loads((ROOT / ".claude/settings.json").read_text(encoding="utf-8"))
    assert settings["plansDirectory"] == "./.agent/scratch/plans"
    assert settings["autoMemoryEnabled"] is False


def test_manager_commits_and_prs_carry_no_claude_attribution() -> None:
    """The owner wants no Co-Authored-By trailer, session link or PR footer on any commit.

    The object form is required: `attribution: false` makes clients older than v2.1.281 skip
    this whole file, and with it every manager hook.
    """
    settings = json.loads((ROOT / ".claude/settings.json").read_text(encoding="utf-8"))
    assert settings["attribution"] == {"commit": "", "pr": "", "sessionUrl": False}
