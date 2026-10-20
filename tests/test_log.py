from pathlib import Path

import pytest

from agent_cli import log as log_mod
from agent_cli.core import AgentError

pytestmark = pytest.mark.usefixtures("notes_remote")


def test_append_is_byte_exact(tmp_root: Path, run):
    run(["feature", "new", "x", "--title", "X"])
    r = run(["--json", "log", "1", "--kind", "decision", "chose argparse"])
    assert r.code == 0
    assert r.json == {"feature": 1, "ts": "2026-09-03 14:05", "kind": "decision", "text": "chose argparse"}
    text = (tmp_root / "features/001-x/log.md").read_text(encoding="utf-8")
    assert text == "# Log — 001 x\n\n> [!decision] 2026-09-03 14:05\n> chose argparse\n"


def test_multiline_and_escape_roundtrip(tmp_root: Path, run):
    run(["feature", "new", "x", "--title", "X"])
    body = "line one\n[!warning] not a nested callout\n\nline after blank\n## looks like a header"
    assert run(["log", "1", "--stdin"], stdin=body).code == 0
    path = tmp_root / "features/001-x/log.md"
    text = path.read_text(encoding="utf-8")
    assert "> \\[!warning] not a nested callout" in text
    assert "> ## looks like a header" in text
    assert "\n>\n" in text  # blank line inside an entry is an empty quote line
    entries = log_mod.parse(path)
    assert len(entries) == 1
    assert entries[0].text == body
    assert entries[0].kind == "note"


def test_two_entries_parse_in_order(tmp_root: Path, run):
    run(["feature", "new", "x", "--title", "X"])
    run(["log", "1", "--kind", "note", "first"])
    run(["log", "1", "--kind", "blocker", "second\nmore"])
    entries = log_mod.parse(tmp_root / "features/001-x/log.md")
    assert [(e.kind, e.text) for e in entries] == [("note", "first"), ("blocker", "second\nmore")]


def test_bad_kind_and_empty_text(tmp_root: Path, run):
    run(["feature", "new", "x", "--title", "X"])
    assert run(["log", "1", "--kind", "bogus", "hi"]).code == 2
    assert run(["log", "1", "   "]).code == 2
    assert (tmp_root / "features/001-x/log.md").read_text(encoding="utf-8") == "# Log — 001 x\n"


def test_cli_refuses_the_cli_owned_kinds_and_matches_the_mcp_enum(tmp_root: Path, run):
    """`bin/agent log --kind handoff` was accepted while the MCP enum already refused
    it, so the same action had two vocabularies. `dispatch` and `handoff` are written by the
    CLI when a worker is sent and when it returns. Neither front end offers them."""
    from agent_cli import log as log_mod
    from agent_cli.mcp.tools import LogKind

    run(["feature", "new", "x", "--title", "X"])
    assert run(["log", "1", "--kind", "handoff", "hi"]).code == 2
    assert run(["log", "1", "--kind", "dispatch", "hi"]).code == 2
    assert run(["log", "1", "--kind", "decision", "hi"]).code == 0
    assert log_mod.MODEL_KINDS == tuple(LogKind.__args__)
    assert set(log_mod.KINDS) - set(log_mod.MODEL_KINDS) == {"dispatch", "handoff"}
    # append() still accepts them: that is how the CLI itself writes a dispatch entry
    from agent_cli import features as features_mod

    entry = log_mod.append(features_mod.resolve(tmp_root, "1"), "handoff", "worker returned")
    assert entry.kind == "handoff"


def test_parse_rejects_malformed(tmp_root: Path, run):
    run(["feature", "new", "x", "--title", "X"])
    path = tmp_root / "features/001-x/log.md"
    base = path.read_text(encoding="utf-8")
    path.write_text(base + "\n> [!vibes] 2026-09-03 14:05\n> text\n", encoding="utf-8")
    with pytest.raises(AgentError, match="malformed"):
        log_mod.parse(path)
    path.write_text(base + "\n## 2026-09-03 14:05 — note\ntext\n", encoding="utf-8")
    with pytest.raises(AgentError, match="callout"):
        log_mod.parse(path)
    path.write_text(base + "\nstray text\n", encoding="utf-8")
    with pytest.raises(AgentError, match="outside an entry"):
        log_mod.parse(path)


def test_log_current_via_session(tmp_root: Path, run):
    run(["feature", "new", "x", "--title", "X"])
    run(["feature", "use", "1", "--session-id", "s1"])
    r = run(["--json", "log", "current", "--session-id", "s1", "via session"])
    assert r.code == 0 and r.json["feature"] == 1
    # outside a Claude session there is no guessing, including from a stale default file
    r = run(["--json", "log", "current", "no session here"])
    assert r.code == 2
    assert "name a feature explicitly" in r.err
    run(["feature", "new", "y", "--title", "Y"])
    (tmp_root / ".agent/default-feature").write_text("2\n")
    assert run(["log", "current", "still gone"]).code == 2
    r = run(["--json", "log", "2", "explicit feature"])
    assert r.code == 0 and r.json["feature"] == 2
