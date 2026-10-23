"""Where a dispatch's machine tag comes from, and what a handoff says about the install that wrote
it (ADR-0036).

The tag is a label the owner sets: `machine:` in agent.local.yaml wins, then the ADR-0035
enrolled device name if it fits `[a-z][a-z0-9]{0,7}`. With neither, `dispatch new` refuses. An
`install:` token, created once under `.agent/`, rides in every new handoff so doctor can spot
two machines sharing one label; it never appears in an id.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agent_cli import dispatch, frontmatter
from agent_cli import features as features_mod
from agent_cli.core import AgentError
from agent_cli.dispatch import handoffs as handoffs_mod
from agent_cli.dispatch import machine
from agent_cli.ops import dispatch as ops_dispatch
from agent_cli.vault import live
from dispatch_helpers import _new_fast, droot, envelope, one, settle  # noqa: F401 — droot is a pytest fixture

HANDOFFS = Path("features/001-alpha-work/handoffs")
OTHER = "feedfacefeedface"  # another install's token
EARLIER, LATER = "2026-09-01T09:00:00-07:00", "2026-09-04T09:00:00-07:00"  # around FIXED_NOW


def _local(root: Path, text: str | None) -> None:
    path = root / "agent.local.yaml"
    if text is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(text, encoding="utf-8")


def _enroll(root: Path, device: str) -> None:
    """The ADR-0035 enrollment store, as `peer-net enroll` leaves it."""
    store = root / ".agent" / "peer-net" / "state.db"
    store.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(store)
    conn.execute("CREATE TABLE IF NOT EXISTS enrollment (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    conn.execute("INSERT OR REPLACE INTO enrollment (k, v) VALUES ('device', ?)", (device,))
    conn.execute("INSERT OR REPLACE INTO enrollment (k, v) VALUES ('enabled', '1')")
    conn.commit()
    conn.close()


@pytest.mark.parametrize(
    ("local", "device", "tag", "source"),
    [
        ("machine: Mac\n", None, "mac", "machine"),
        ("machine: win2\n", "studio", "win2", "machine"),
        (None, "Studio", "studio", "device"),
        ("notes_sync: off\n", "ab12", "ab12", "device"),
    ],
)
def test_tag_resolution(tmp_root: Path, local, device, tag, source):
    _local(tmp_root, local)
    if device:
        _enroll(tmp_root, device)
    resolved = machine.resolve(tmp_root)
    assert (resolved.tag, resolved.source, resolved.problem) == (tag, source, None)
    assert machine.require_tag(tmp_root) == tag


@pytest.mark.parametrize(
    ("local", "device", "names"),
    [
        (None, None, "machine:"),
        (None, "mac-mini", "mac-mini"),
        (None, "averylongname", "averylongname"),
        ("machine: my-mac\n", None, "my-mac"),
        ("machine: 1mac\n", "studio", "1mac"),
        ("machine: abcdefghi\n", None, "abcdefghi"),
        # a present but null or non-string override refuses; it never falls through to the device
        ("machine: null\n", "studio", "machine: None"),
        ("machine:\n", "studio", "machine: None"),
        ("machine: 42\n", "studio", "machine: 42"),
    ],
)
def test_dispatch_new_refuses_without_a_valid_tag_and_names_the_fix(droot: Path, local, device, names):
    _local(droot, local)
    if device:
        _enroll(droot, device)
    with pytest.raises(AgentError) as caught:
        _new_fast(droot)
    message = caught.value.message
    assert "agent.local.yaml" in message and "machine:" in message and names in message
    assert not list(dispatch.dispatches_dir(droot).glob("*.json"))


def test_handoffs_carry_machine_and_one_install_token(droot: Path, fake_claude):
    fake_claude.scenario([one(envelope()), one(envelope())])
    first = _new_fast(droot)
    settle(droot, first)
    second = _new_fast(droot)
    stored = json.loads((droot / ".agent" / "install.json").read_text(encoding="utf-8"))
    token = stored["token"]
    assert len(token) >= 16 and stored["created"]
    for d in (first, second):
        meta, _ = frontmatter.load(droot / d["handoff"])
        assert (meta["machine"], meta["install"]) == ("mac", token)
        assert token not in d["id"] and token not in d["branch"] and token not in d["worktree"]
    assert machine.install(droot)["token"] == token  # created once, read after


def _foreign(root: Path, name: str, dispatch_id: str, *, install: str, created: str, status: str = "done"):
    meta = {
        "id": dispatch_id,
        "feature": 1,
        "repo": "alpha-api",
        "session_id": f"other-{dispatch_id}",
        "status": status,
        "machine": dispatch_id.split("-")[1],
        "install": install,
        "created": created,
        "tags": ["handoff"],
    }
    (root / HANDOFFS).mkdir(parents=True, exist_ok=True)
    (root / HANDOFFS / name).write_text(frontmatter.join(meta, "# Handoff\n"), encoding="utf-8")


def test_reinstall_cannot_reuse_a_historical_label(droot: Path, fake_claude):
    """Clock order does not authorize reusing another installation's historical label."""
    fake_claude.scenario([one(envelope())])
    _foreign(droot, "mac-01-alpha.md", "001-mac-01", install="0ldt0ken0ldt0ken", created=EARLIER)
    with pytest.raises(AgentError, match="legacy handoff"):
        _new_fast(droot)
    assert not dispatch.load_all(droot)


def test_a_tagged_handoff_with_no_local_record_has_no_local_runtime(droot: Path):
    _foreign(droot, "win-01-alpha.md", "001-win-01", install=OTHER, created=LATER, status="running")
    shown = ops_dispatch.dispatch_show(droot, dispatch_id="001-win-01")
    assert shown.data["local_runtime"] is False
    assert shown.data["machine"] == "win"
    assert "no local runtime" in shown.text and "win" in shown.text


def test_render_over_legacy_and_tagged_handoffs(droot: Path, fake_claude, run):
    """A vault mid-rollout: an old `001-01` dispatch and a new tagged one. Dashboard and Workers
    both read both."""
    fake_claude.scenario([one(envelope())])
    tagged = _new_fast(droot)
    feat = features_mod.resolve(droot, "1")
    legacy = {
        **dispatch.load(droot, tagged["id"]),
        "id": "001-01",
        "handoff": str(HANDOFFS / "01-alpha.md"),
        "session_id": "legacy-session",
        "record_revision": 0,
    }
    for key in ("machine", "install"):
        legacy.pop(key, None)
    dispatch.state.save(droot, legacy)
    handoffs_mod.write_handoff(droot, legacy, feat)
    run(["status", "render"])
    dashboard = (droot / "Dashboard.md").read_text(encoding="utf-8")
    assert "```agent-live\n```" in dashboard
    assert {w["id"] for w in live.build(droot)["workers"]} == {"001-01", tagged["id"]}
    plan = (droot / "features/001-alpha-work/plan.md").read_text(encoding="utf-8")
    assert 'await dv.view("harness/obsidian/views/handoffs")' in plan
