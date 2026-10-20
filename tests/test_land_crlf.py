"""Landing and recovery observe explicitly normalised CRLF text without running filters."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_cli import land, land_recover
from agent_cli import land_journal as jn
from agent_cli import land_observe as obs
from agent_cli.core import AgentError
from land_helpers import certified, child, fingerprint, git, harness, only_journal, recorded  # noqa: F401


def _crlf_branch(
    root: Path,
    attributes: str = "*.cmd text eol=crlf\n*.ps1 text eol=crlf\n",
    candidate_attributes: str | None = None,
) -> None:
    (root / ".gitattributes").write_text(attributes)
    for name in ("launch.cmd", "launch.ps1"):
        (root / "bin" / name).write_bytes(b"old\n")
    git(root, "add", ".gitattributes", "bin")
    git(root, "commit", "-q", "-m", "CRLF launchers")
    git(root, "branch", "-f", "feature", "main")
    git(root, "switch", "-q", "feature")
    if candidate_attributes is not None:
        (root / ".gitattributes").write_text(candidate_attributes)
    for name in ("launch.cmd", "launch.ps1"):
        (root / "bin" / name).write_bytes(b"candidate\r\n")
    git(root, "commit", "-qam", "change launchers")
    git(root, "switch", "-q", "main")


@pytest.mark.parametrize("point", ["files_applied", "index_renamed"])
def test_crlf_crash_recovery_undoes_and_finishes(harness: Path, monkeypatch: pytest.MonkeyPatch, point: str):
    """After the files_applied crash, a lone CR or an attribute change made since refuses
    recovery with zero changes; put back, recovery undoes and finishes."""
    _crlf_branch(harness)
    recorded(harness)
    assert child(harness, f"{point}:exit").wait(60) == 97
    journal = only_journal(harness)
    git_obs = obs.Git(harness)
    assert set(land_recover.observe(harness, git_obs, journal)["classes"].values()) == {obs.AT_CAND}
    if point == "files_applied":
        for path, edit in (
            (harness / "bin" / "launch.cmd", b"candidate\r"),
            (harness / ".gitattributes", b"*.cmd text=auto eol=crlf\n*.ps1 text eol=crlf\n"),
        ):
            kept = path.read_bytes()
            path.write_bytes(edit)
            before = fingerprint(harness)
            with pytest.raises(AgentError, match="changed after the landing began"):
                land_recover.recover(harness, journal["id"])
            assert fingerprint(harness) == before
            assert jn.lock_state(harness, journal) == "ours"
            path.write_bytes(kept)
    undo = land_recover.undo
    undid: list[bool] = []

    def checked_undo(main: Path, git: obs.Git, journal: dict[str, Any]) -> None:
        undo(main, git, journal)
        undid.append(True)
        assert (main / "bin" / "launch.cmd").read_bytes() == b"old\r\n"
        assert set(land_recover.observe(main, git, journal)["classes"].values()) == {obs.AT_OLD}

    monkeypatch.setattr(land_recover, "undo", checked_undo)
    assert land_recover.recover(harness, journal["id"])["state"] == "done"
    assert bool(undid) == (point == "files_applied")
    assert git(harness, "rev-parse", "main") == journal["cand"]
    assert (harness / "bin" / "launch.cmd").read_bytes() == b"candidate\r\n"
    assert not jn.lock_file(harness).exists()


def test_explicit_crlf_text_edits_additions_and_deletions_land(harness: Path, certified: None):
    _crlf_branch(harness)
    assert (harness / "bin" / "launch.cmd").read_bytes() == b"old\r\n"
    git(harness, "switch", "-q", "feature")
    (harness / "bin" / "new.cmd").write_bytes(b"new\r\n")
    (harness / "bin" / "launch.ps1").unlink()
    git(harness, "add", "bin")
    git(harness, "commit", "-q", "-m", "add and remove CRLF files")
    git(harness, "switch", "-q", "main")
    journal = land.land(harness, "feature", strategy="ff")
    assert journal["state"] == "done"
    assert git(harness, "rev-parse", "main") == journal["cand"]
    assert (harness / "bin" / "launch.cmd").read_bytes() == b"candidate\r\n"
    assert (harness / "bin" / "new.cmd").read_bytes() == b"new\r\n"
    assert not (harness / "bin" / "launch.ps1").exists()
    git_obs = obs.Git(harness)
    assert land.verify_done(harness, git_obs, journal) == []
    assert set(land_recover.observe(harness, git_obs, journal)["classes"].values()) == {obs.AT_CAND}


@pytest.mark.parametrize("text", ["text=auto", "-text", "!text"])
def test_crlf_without_explicit_text_is_refused(harness: Path, text: str):
    _crlf_branch(harness, f"*.cmd {text} eol=crlf\n*.ps1 text eol=crlf\n")
    before = fingerprint(harness)
    with pytest.raises(AgentError, match="eol: crlf"):
        land.land(harness, "feature", strategy="ff")
    assert fingerprint(harness) == before


@pytest.mark.parametrize("rev", ["main", "feature"])
def test_cr_in_either_blob_is_refused(harness: Path, rev: str):
    _crlf_branch(harness)
    git(harness, "switch", "-q", rev)
    (harness / "bin" / "launch.cmd").write_bytes(b"raw\xff\r\n")
    oid = git(harness, "hash-object", "-w", "--no-filters", "bin/launch.cmd")
    git(harness, "update-index", "--cacheinfo", f"100644,{oid},bin/launch.cmd")
    git(harness, "commit", "-q", "-m", "unnormalised blob")
    git(harness, "switch", "-q", "-f", "main")
    git_obs = obs.Git(harness)
    old, cand = land.entries(git_obs, {"old": "main", "cand": "feature"})
    why, crlf = obs.unsupported(git_obs, ["bin/launch.cmd"], old, cand, ("main", "feature"))
    assert why and not crlf
    before = fingerprint(harness)
    with pytest.raises(AgentError, match="eol: crlf"):
        land.check_prepared(harness, git_obs, {"old": "main", "cand": "feature", "paths": ["bin/launch.cmd"]})
    assert fingerprint(harness) == before


@pytest.mark.parametrize("attribute", ["filter=probe", "ident", "working-tree-encoding=UTF-16"])
def test_crlf_with_another_conversion_is_refused(harness: Path, attribute: str):
    _crlf_branch(harness)
    (harness / ".git" / "info").mkdir(exist_ok=True)
    (harness / ".git" / "info" / "attributes").write_text(f"*.cmd text eol=crlf {attribute}\n")
    before = fingerprint(harness)
    with pytest.raises(AgentError, match="cannot move main safely"):
        land.land(harness, "feature", strategy="ff")
    assert fingerprint(harness) == before


@pytest.mark.parametrize("candidate_attributes", ["*.cmd text eol=lf\n", "*.cmd text=auto eol=crlf\n"])
def test_disagreeing_old_and_candidate_attributes_are_refused(harness: Path, candidate_attributes: str):
    _crlf_branch(harness, candidate_attributes=candidate_attributes)
    before = fingerprint(harness)
    with pytest.raises(AgentError, match="eol: crlf"):
        land.land(harness, "feature", strategy="ff")
    assert fingerprint(harness) == before


@pytest.mark.parametrize("data", [b"old\r", b"old\r\r\n", b"changed\r\n"])
def test_a_lone_cr_or_changed_worktree_content_is_other(harness: Path, data: bytes):
    _crlf_branch(harness)
    (harness / "bin" / "launch.cmd").write_bytes(data)
    git_obs = obs.Git(harness)
    old, cand = land.entries(git_obs, {"old": "main", "cand": "feature"})
    why, crlf = obs.unsupported(git_obs, ["bin/launch.cmd"], old, cand, ("main", "feature"))
    assert not why and crlf == {"bin/launch.cmd"}
    assert obs.classify(harness, ["bin/launch.cmd"], old, cand, "sha1", crlf=crlf) == {
        "bin/launch.cmd": obs.OTHER
    }
    before = fingerprint(harness)
    with pytest.raises(AgentError, match="differ from main"):
        land.land(harness, "feature", strategy="ff")
    assert fingerprint(harness) == before
