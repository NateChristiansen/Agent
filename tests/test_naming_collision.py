"""Two Claude managers on one feature get distinct names.

On 2026-09-25 a fresh Claude session ran feature_use 034 and took the running 034 manager's
name. `peer send mgr-034-harness-cleanup` became ambiguous, and a Codex reply meant for one
reached the other.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_cli import obsidian_tabs, ops, sessions
from agent_cli.hooks import naming

pytestmark = pytest.mark.usefixtures("notes_remote")

FEATURE = {"id": 34, "slug": "harness-cleanup", "dir": "features/034-harness-cleanup"}


def test_the_second_manager_on_a_feature_gets_a_suffixed_name(tmp_root: Path, monkeypatch):
    monkeypatch.setattr("agent_cli.core.is_alive", lambda pid: True)
    sessions.set_record(
        tmp_root, "77c11741-aaaa", name="mgr-034-harness-cleanup", feature=FEATURE, pid=os.getpid()
    )
    second = {"session_id": "520da25c-bbbb", "feature": FEATURE}
    taken = naming.taken_names(tmp_root, "520da25c-bbbb")
    assert naming.desired_title(second, "520da25c-bbbb", taken=taken) == "mgr-034-harness-cleanup-520da2"
    # the first manager keeps its name; a lone manager has no suffix
    first = sessions.get(tmp_root, "77c11741-aaaa")
    assert naming.desired_title(
        first, "77c11741-aaaa", taken=naming.taken_names(tmp_root, "77c11741-aaaa")
    ) == ("mgr-034-harness-cleanup")


def test_a_suffixed_name_is_kept_after_the_first_manager_leaves(tmp_root: Path):
    record = {"session_id": "520da25c-bbbb", "feature": FEATURE, "name": "mgr-034-harness-cleanup-520da2"}
    assert naming.desired_title(record, "520da25c-bbbb", taken=set()) == "mgr-034-harness-cleanup-520da2"


def test_both_managers_are_addressable_by_name(tmp_root: Path, monkeypatch):
    monkeypatch.setattr("agent_cli.ops.peers.is_alive", lambda pid: True)
    sessions.set_record(tmp_root, "77c11741-aaaa", name="mgr-034-harness-cleanup", pid=os.getpid())
    sessions.set_record(tmp_root, "520da25c-bbbb", name="mgr-034-harness-cleanup-520da2", pid=os.getpid())
    assert ops.peers.resolve(tmp_root, "mgr-034-harness-cleanup") == "77c11741-aaaa"
    assert ops.peers.resolve(tmp_root, "mgr-034-harness-cleanup-520da2") == "520da25c-bbbb"


def test_a_suffixed_manager_remapped_to_another_feature_takes_that_features_name(
    tmp_root: Path, run, monkeypatch
):
    """Found in review: feature_use compared the old name against the unsuffixed title, so a
    suffixed manager kept its old name after moving to another feature."""
    alpha = ops.feature_new(tmp_root, slug="alpha", title="Alpha", use=False).data
    beta = ops.feature_new(tmp_root, slug="beta", title="Beta", use=False).data
    old = f"mgr-{alpha['number']}-alpha-520da2"
    mapped = {"id": alpha["id"], "slug": "alpha", "dir": alpha["dir"]}
    sessions.set_record(tmp_root, "520da25c-bbbb", feature=mapped, name=old)
    ops.feature_use(tmp_root, feature=str(beta["id"]), session_id="520da25c-bbbb")
    assert (sessions.get(tmp_root, "520da25c-bbbb") or {})["name"] == f"mgr-{beta['number']}-beta"


def test_the_tab_drops_the_suffix_it_already_shows():
    display = obsidian_tabs.manager_display_name(
        "mgr-034-harness-cleanup-520da2", "520da25c-bbbb", "claude", generated=True
    )
    assert display.startswith("034 ") and "520da2" not in display.split(" · ")[0]
    assert display.endswith(" · cl 520d")
