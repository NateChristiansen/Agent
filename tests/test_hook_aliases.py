"""A hook name wired at the slice base keeps resolving for one release.

A client session keeps the hook wiring it read at start. An earlier change deleted `done-unticked`, the
CLI parser exited 2 on it, and a Codex manager started before that landing looped 84 Stops in
four minutes. `hooks.run.ALIASES` keeps a removed or renamed name as a no-op; `hook_names.json`
is the base's list, and this test fails when a name leaves it with no alias."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_cli.cli import parse
from agent_cli.hooks.groups import NAMES as GROUP_NAMES
from agent_cli.hooks.run import ALIASES, NAMES, run

BASE = json.loads((Path(__file__).resolve().parent / "hook_names.json").read_text(encoding="utf-8"))
STOP = json.dumps({"hook_event_name": "Stop", "session_id": "s", "stop_hook_active": True})


def test_every_name_wired_at_the_slice_base_still_resolves():
    known = {*NAMES, *GROUP_NAMES, *ALIASES}
    missing = sorted({*BASE["hooks"], *BASE["groups"]} - known)
    assert not missing, f"removed or renamed with no alias; add each to hooks.run.ALIASES: {missing}"


def test_an_alias_is_never_also_a_live_name():
    assert not set(ALIASES) & {*NAMES, *GROUP_NAMES}
    assert all(ALIASES.values()), "each alias names the release that removed the hook"


@pytest.mark.parametrize("name", sorted(ALIASES))
def test_an_alias_is_a_silent_no_op(tmp_root: Path, name: str):
    assert run(tmp_root, name, STOP) == ("", 0)
    assert parse(["hook", name]).name == name  # the full CLI parser accepts it too


def test_an_alias_through_the_console_entry_exits_clean(tmp_root: Path):
    """The path a Codex Stop hook takes: a fresh interpreter, `agent hook <name>`."""
    done = subprocess.run(
        [sys.executable, "-m", "agent_cli", "hook", "done-unticked"],
        input=STOP,
        capture_output=True,
        text=True,
        cwd=tmp_root,
        env={**os.environ, "AGENT_ROOT": str(tmp_root)},
    )
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
