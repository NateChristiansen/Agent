"""The handoff ownership guard: a dispatch record writes its handoff, its live
fields and its plan line only when the handoff file is absent or carries the record's own
nonblank `session_id` (and, where present, the same id, feature and repo). Every other case
skips and is reported: by the writer's return value, by `sync_live_fields(skipped=...)`, and by
a doctor warning on the record. The guard contains id collisions; it does not fix allocation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_cli import dispatch, frontmatter
from agent_cli import features as features_mod
from agent_cli.dispatch import handoffs as handoffs_mod
from agent_cli.dispatch import ownership
from dispatch_helpers import _new_fast, droot, envelope, one  # noqa: F401 -- droot is a fixture


@pytest.fixture
def rec(droot: Path, fake_claude) -> dict:
    fake_claude.scenario([one(envelope())])
    d = _new_fast(droot)
    return dispatch.load(droot, d["id"])


def _rewrite_meta(path: Path, **changes) -> str:
    meta, body = frontmatter.load(path)
    for k, v in changes.items():
        if v is ownership.MISSING:
            meta.pop(k, None)
        else:
            meta[k] = v
    text = frontmatter.join(meta, body)
    path.write_text(text, encoding="utf-8")
    return text


def _feat(root: Path) -> features_mod.Feature:
    return features_mod.resolve(root, "1")


def _all_writers_skip(root: Path, d: dict, path: Path, text: str) -> list[dict]:
    """Handoff writers leave foreign files and every plan alone."""
    plan_before = _feat(root).body
    assert handoffs_mod.write_handoff(root, {**d, "state": "abandoned"}, _feat(root)) is None
    skipped: list[dict] = []
    handoffs_mod.sync_live_fields(root, skipped=skipped)
    assert path.read_text(encoding="utf-8") == text
    assert _feat(root).body == plan_before
    return skipped


def test_matching_token_or_absent_file_is_written(droot: Path, rec: dict):
    path = droot / rec["handoff"]
    rec["state"] = "abandoned"
    assert handoffs_mod.write_handoff(droot, rec, _feat(droot)) == path
    assert frontmatter.load(path)[0]["status"] == "abandoned"
    assert "— abandoned" not in _feat(droot).body

    path.unlink()
    assert ownership.foreign_reason(path, rec) is None
    assert handoffs_mod.write_handoff(droot, rec, _feat(droot)) == path
    assert frontmatter.load(path)[0]["session_id"] == rec["session_id"]


# Every skip leaves the file as it was, so one dispatch serves each case in turn.
def test_foreign_or_unreadable_files_skip_and_report(droot: Path, rec: dict):
    path = droot / rec["handoff"]
    original = path.read_text(encoding="utf-8")

    text = _rewrite_meta(path, session_id="another-machines-session", goal="theirs")
    skipped = _all_writers_skip(droot, rec, path, text)
    assert skipped == [
        {"id": rec["id"], "handoff": rec["handoff"], "reason": ownership.foreign_reason(path, rec)}
    ]
    assert "another-machines-session" in skipped[0]["reason"]

    # A legacy handoff without the token, and blank tokens, are not adopted.
    for token in (ownership.MISSING, "", None, "   "):
        path.write_text(original, encoding="utf-8")
        text = _rewrite_meta(path, session_id=token)
        skipped = _all_writers_skip(droot, rec, path, text)
        assert [s["handoff"] for s in skipped] == [rec["handoff"]], token

    # None == None and "" == "" are not a match: the record must carry a token too.
    for token in ("", None):
        path.write_text(original, encoding="utf-8")
        text = _rewrite_meta(path, session_id=token)
        assert ownership.foreign_reason(path, {**rec, "session_id": token}) is not None
        assert handoffs_mod.write_handoff(droot, {**rec, "session_id": token}, _feat(droot)) is None
        assert path.read_text(encoding="utf-8") == text

    for key, value in (("id", "001-02"), ("feature", "002"), ("repo", "beta")):
        path.write_text(original, encoding="utf-8")
        text = _rewrite_meta(path, **{key: value})
        reason = ownership.foreign_reason(path, rec)
        assert reason is not None and key in reason
        _all_writers_skip(droot, rec, path, text)

    for text in (
        "no frontmatter at all\n",
        "---\nsession_id: [unclosed\n---\n\nbody\n",
        "---\n- a list\n---\n\nbody\n",
    ):
        path.write_text(text, encoding="utf-8")
        skipped = _all_writers_skip(droot, rec, path, text)
        assert skipped and "unreadable" in skipped[0]["reason"], text

    raw = b"---\nsession_id: \xff\xfe\n---\n"
    path.write_bytes(raw)
    assert "unreadable" in (ownership.foreign_reason(path, rec) or "")
    assert handoffs_mod.write_handoff(droot, rec, _feat(droot)) is None
    assert path.read_bytes() == raw


def test_git_text_with_an_invalid_byte_is_written_as_replacement(droot: Path, rec: dict):
    path = droot / rec["handoff"]
    bad_name, other_name = "dir/a\udcffb", "dir/a\udcfeb"
    rec["state"] = "done"
    rec["result"] = {"status": "done", "summary": "s"}
    rec["git"] = {
        "commits": [{"sha": "abcdef0123", "subject": "fix \udcff subject"}],
        "files": [bad_name, other_name],
    }
    assert handoffs_mod.write_handoff(droot, rec, _feat(droot)) == path
    text = path.read_bytes().decode("utf-8")
    assert "fix � subject" in text and "dir/a�b" in text
    assert "\udcff" not in text
    assert "fix � subject" in handoffs_mod.handoff_log_text(droot, rec)


def test_git_names_differing_in_an_invalid_byte_stay_distinct(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from agent_cli.dispatch import worktrees

    def fake_git(args: list[str], cwd: Path) -> str:
        if args[0] in ("diff", "status"):
            return "dir/a\udcffb\ndir/a\udcfeb\n" if args[0] == "diff" else ""
        return "abc\tp\tsubject \udcff\n"

    monkeypatch.setattr(worktrees.repos_mod, "git_or_none", fake_git)
    assert worktrees.git_files(tmp_path, "base") == ["dir/a\udcffb", "dir/a\udcfeb"]
    assert worktrees.git_commits(tmp_path, "base")[0]["subject"] == "subject \udcff"
