"""`session export` copies `.agent/` evidence out of a clone.

`.agent/` is machine-local, so deleting a clone deleted its records; a Windows run had to copy
them out by hand before a wipe.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from agent_cli import evidence, ops
from agent_cli.core import AgentError

pytestmark = pytest.mark.usefixtures("notes_remote")


def _seed(root: Path) -> Path:
    agent = root / ".agent"
    (agent / "sessions" / "by-pid").mkdir(parents=True)
    (agent / "sessions" / "s1.json").write_text('{"session_id": "s1"}\n', encoding="utf-8")
    (agent / "sessions" / "by-pid" / "123").write_text("s1\n", encoding="utf-8")
    for feature in ("030-example-feature", "031-other"):
        (agent / "raw" / feature).mkdir(parents=True)
        (agent / "raw" / feature / "2026-09-27.jsonl").write_text(f"{feature}\n", encoding="utf-8")
    # any snapshot write before the seed has made it already: it holds `live.PUBLISH_LOCK`
    (agent / "tabs").mkdir(exist_ok=True)
    (agent / "tabs" / "claim-failures.jsonl").write_text("{}\n", encoding="utf-8")
    (agent / "live.json").write_text("{}\n", encoding="utf-8")
    # out of scope: an executable environment and drafts
    (agent / "terminal-python" / "Scripts").mkdir(parents=True)
    (agent / "terminal-python" / "Scripts" / "python.exe").write_bytes(b"MZ")
    (agent / "scratch").mkdir()
    (agent / "scratch" / "draft.md").write_text("draft\n", encoding="utf-8")
    return agent


def _copied(dest: Path) -> set[str]:
    return {row["path"] for row in json.loads((dest / "manifest.json").read_text())["files"]}


def test_export_copies_the_scope_with_hashes_and_leaves_the_originals(tmp_root: Path, tmp_path: Path) -> None:
    agent = _seed(tmp_root)
    before = {p: p.read_bytes() for p in agent.rglob("*") if p.is_file()}
    dest = tmp_path / "saved"
    result = evidence.export(tmp_root, dest)
    copied = _copied(dest)
    assert "sessions/s1.json" in copied and "sessions/by-pid/123" in copied
    assert {"raw/030-example-feature/2026-09-27.jsonl", "raw/031-other/2026-09-27.jsonl"} <= copied
    assert "tabs/claim-failures.jsonl" in copied and "live.json" in copied
    assert not any(p.startswith(("terminal-python", "scratch")) for p in copied)
    assert result["files"] == len(copied)
    for row in json.loads((dest / "manifest.json").read_text())["files"]:
        data = (dest / ".agent" / row["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == row["sha256"] and len(data) == row["bytes"]
    assert {p: p.read_bytes() for p in agent.rglob("*") if p.is_file()} == before


def test_feature_limits_raw_captures_only(tmp_root: Path, tmp_path: Path) -> None:
    _seed(tmp_root)
    dest = tmp_path / "saved"
    evidence.export(tmp_root, dest, feature_dir="030-example-feature")
    copied = _copied(dest)
    assert "raw/030-example-feature/2026-09-27.jsonl" in copied
    assert not any(p.startswith("raw/031-other") for p in copied)
    assert "sessions/s1.json" in copied


@pytest.mark.parametrize("where", ["inside", "nonempty"])
def test_export_refuses_a_bad_destination(tmp_root: Path, tmp_path: Path, where: str) -> None:
    _seed(tmp_root)
    dest = tmp_root / "evidence" if where == "inside" else tmp_path / "used"
    if where == "nonempty":
        dest.mkdir()
        (dest / "keep.txt").write_text("mine\n", encoding="utf-8")
    with pytest.raises(AgentError):
        evidence.export(tmp_root, dest)
    if where == "nonempty":
        assert (dest / "keep.txt").read_text() == "mine\n" and not (dest / "manifest.json").exists()


def test_symlinks_are_not_followed_out_of_scope(tmp_root: Path, tmp_path: Path) -> None:
    agent = _seed(tmp_root)
    outside = tmp_path / "secret.txt"
    outside.write_text("not evidence\n", encoding="utf-8")
    try:
        (agent / "tabs" / "link.txt").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks need a privilege this platform lacks")
    dest = tmp_path / "saved"
    evidence.export(tmp_root, dest)
    assert "tabs/link.txt" not in _copied(dest)


def test_a_linked_raw_folder_is_not_read_even_for_one_feature(tmp_root: Path, tmp_path: Path) -> None:
    agent = _seed(tmp_root)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "030-example-feature").mkdir(parents=True)
    (elsewhere / "030-example-feature" / "sentinel.jsonl").write_text("outside\n", encoding="utf-8")
    import shutil

    shutil.rmtree(agent / "raw")
    try:
        (agent / "raw").symlink_to(elsewhere, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks need a privilege this platform lacks")
    dest = tmp_path / "saved"
    evidence.export(tmp_root, dest, feature_dir="030-example-feature")
    assert not any(p.startswith("raw/") for p in _copied(dest))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junctions")
def test_a_junction_raw_folder_is_not_read_even_for_one_feature(tmp_root: Path, tmp_path: Path) -> None:
    import _winapi  # type: ignore[import-not-found]
    import shutil

    agent = _seed(tmp_root)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "030-example-feature").mkdir(parents=True)
    (elsewhere / "030-example-feature" / "sentinel.jsonl").write_text("outside\n", encoding="utf-8")
    shutil.rmtree(agent / "raw")
    raw = str(agent / "raw")
    _winapi.CreateJunction(str(elsewhere), raw)  # pyright: ignore[reportAttributeAccessIssue] — win32 only
    dest = tmp_path / "saved"
    evidence.export(tmp_root, dest, feature_dir="030-example-feature")
    assert not any(p.startswith("raw/") for p in _copied(dest))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junctions")
def test_a_junction_is_not_followed_out_of_scope(tmp_root: Path, tmp_path: Path) -> None:
    import _winapi  # type: ignore[import-not-found]

    agent = _seed(tmp_root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel.txt").write_text("not evidence\n", encoding="utf-8")
    junction = str(agent / "tabs" / "junction")
    _winapi.CreateJunction(str(outside), junction)  # pyright: ignore[reportAttributeAccessIssue] — win32 only
    dest = tmp_path / "saved"
    evidence.export(tmp_root, dest)
    assert not any("sentinel" in p or p.startswith("tabs/junction") for p in _copied(dest))
    assert (outside / "sentinel.txt").read_text() == "not evidence\n"


def test_an_unreadable_link_status_is_treated_as_a_link(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "x"
    target.write_text("x\n", encoding="utf-8")

    def boom(path):
        raise OSError("denied")

    monkeypatch.setattr(evidence.os, "lstat", boom)
    assert evidence._is_link(target) is True


def test_the_op_resolves_a_feature_ref(tmp_root: Path, tmp_path: Path, run) -> None:
    assert run(["feature", "new", "thing", "--title", "Thing"]).code == 0
    agent = _seed(tmp_root)
    (agent / "raw" / "001-thing").mkdir()
    (agent / "raw" / "001-thing" / "a.jsonl").write_text("x\n", encoding="utf-8")
    op = ops.session_export(tmp_root, dest=str(tmp_path / "saved"), feature="1")
    assert op.data["files"] >= 1 and "exported" in op.text
    assert "raw/001-thing/a.jsonl" in _copied(tmp_path / "saved")
