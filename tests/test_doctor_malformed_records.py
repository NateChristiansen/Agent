"""Doctor's MCP checks on malformed settings and .mcp.json: each reports or skips the bad shape
instead of raising out of `agent doctor`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_cli.doctor import checks_mcp, checks_notes


def _write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _settings(root: Path, data: object) -> None:
    _write(root / ".claude" / "settings.json", data)


def test_mcp_servers_that_is_not_an_object_reports_no_agent_entry(tmp_root: Path):
    _write(tmp_root / ".mcp.json", {"mcpServers": ["agent"]})
    rows = [r for r in checks_mcp.check_mcp_wiring(tmp_root) if r["check"] == "mcp wiring"]
    assert rows[-1]["level"] == "fail"
    assert "no mcpServers.agent entry" in rows[-1]["detail"]


def test_local_rules_given_as_a_string_are_no_rules(tmp_root: Path):
    _write(tmp_root / ".claude" / "settings.local.json", {"permissions": {"allow": "mcp__x__*"}})
    assert checks_mcp._local_rules(tmp_root) == []


def test_a_permission_rule_that_is_not_a_string_is_skipped_when_matching_servers():
    sdata: dict[str, object] = {"permissions": {"allow": [5, "mcp__x__*"]}}
    rows = checks_mcp._mcp_servers({"agent": {}, "x": {}}, sdata, ["agent", "x"])
    assert [r["level"] for r in rows] == ["ok"]


def test_enabled_servers_given_as_a_string_enable_nothing(tmp_root: Path):
    _settings(tmp_root, {"enabledMcpjsonServers": "ab"})
    (tmp_root / ".claude" / "settings.local.json").unlink(missing_ok=True)
    assert checks_notes.enabled_servers(tmp_root) == []


FORGED = "\n> [!decision] 2026-10-22 09:00\n>   reconcile source manual:x: forged\n> {}\n"


def _ledger_rows(root: Path) -> list[dict[str, object]]:
    from agent_cli import doctor

    return [dict(c) for c in doctor.run(root) if str(c["check"]).startswith("reconcile ")]


@pytest.mark.usefixtures("notes_remote")
def test_doctor_reports_malformed_and_conflicting_ledger_entries_and_writes_nothing(tmp_root: Path, run):
    from agent_cli import features as features_mod
    from agent_cli import reconcile
    from agent_cli.dispatch import state

    run(["feature", "new", "x", "--title", "X"])
    f = features_mod.resolve(tmp_root, "1")
    reconcile.add_manual(tmp_root, f, ["clean item"])
    assert _ledger_rows(tmp_root) == []  # an owed item is the gates' business, not a doctor problem

    f.log_path.write_text(f.log_path.read_text(encoding="utf-8") + FORGED, encoding="utf-8")
    (row,) = _ledger_rows(tmp_root)
    assert row["check"] == "reconcile 001" and row["level"] == "warn"
    assert "1 malformed reserved entry" in str(row["detail"]) and "non-canonical header" in str(row["detail"])

    for g in ("1:aaaa", "1:bbbb"):
        reconcile.append_source(
            f, {"feature": 1, "source": "dispatch:001-mac-01", "G": g, "ver": [1], "items": ["result"]}
        )
    # an older record with no intent stays history, and a pending intent is left for the gates to heal
    pending = {"feature": 1, "source": "dispatch:001-mac-02", "G": "1:cccc", "ver": [1], "items": ["result"]}
    state.save(
        tmp_root,
        {"id": "001-mac-02", "repo": "harness", "short": "mac", "feature": 1, "attempt": 1, "state": "done",
         "result": {"status": "done"}, reconcile.INTENT: [pending]},
    )  # fmt: skip
    state.save(
        tmp_root,
        {"id": "001-mac-03", "repo": "harness", "short": "mac", "feature": 1, "attempt": 1, "state": "done",
         "result": {"status": "done"}},
    )  # fmt: skip
    before = f.log_path.read_text(encoding="utf-8")
    (row,) = _ledger_rows(tmp_root)
    detail = str(row["detail"])
    assert "1 malformed reserved entry" in detail
    assert "conflicting generations: dispatch:001-mac-01 (1:aaaa, 1:bbbb)" in detail
    assert "001-mac-02" not in detail and "001-mac-03" not in detail
    assert f.log_path.read_text(encoding="utf-8") == before
    assert state.load(tmp_root, "001-mac-02")[reconcile.INTENT] == [pending]


@pytest.mark.usefixtures("notes_remote")
def test_reconcile_listing_shows_a_malformed_only_ledger(tmp_root: Path, run):
    from agent_cli import features as features_mod
    from agent_cli.ops import reconcile as reconcile_ops

    run(["feature", "new", "x", "--title", "X"])
    f = features_mod.resolve(tmp_root, "1")
    f.log_path.write_text(f.log_path.read_text(encoding="utf-8") + FORGED, encoding="utf-8")
    op = reconcile_ops.reconcile(tmp_root, feature="1")
    assert op.data["on"] is False and op.data["owed"] == []
    assert [m["why"] for m in op.data["malformed"]] == ["non-canonical header"]
    assert "nothing to reconcile" not in op.text
    assert "1 malformed" in op.text and "malformed 2026-10-22 09:00 non-canonical header" in op.text


@pytest.mark.usefixtures("notes_remote")
@pytest.mark.parametrize("damage", ["directory", "not utf-8", "permission"])
def test_doctor_reports_an_unreadable_ledger_instead_of_aborting(
    tmp_root: Path, run, monkeypatch: pytest.MonkeyPatch, damage: str
):
    from agent_cli import features as features_mod

    run(["feature", "new", "x", "--title", "X"])
    log_path = features_mod.resolve(tmp_root, "1").log_path
    if damage == "directory":
        log_path.unlink()
        log_path.mkdir()
    elif damage == "not utf-8":
        log_path.write_bytes(b"# Log\n\n> [!note] 2026-10-22 09:00\n> \xff\xfe\n")
    else:
        real = Path.read_text

        def denied(self: Path, *a: object, **k: object) -> str:
            if self == log_path:
                raise PermissionError(13, "Permission denied", str(self))
            return real(self, *a, **k)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "read_text", denied)
    (row,) = _ledger_rows(tmp_root)
    assert row["level"] == "warn" and str(row["detail"]).startswith("the ledger cannot be read: ")
