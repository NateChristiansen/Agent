"""What `agent land` refuses before it writes anything: configurations whose checked-out bytes are
not the blob bytes, and any filter. Observation itself never runs a configured program."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from agent_cli import land, land_recover
from agent_cli import land_journal as jn
from agent_cli import land_observe as obs
from agent_cli.core import AgentError
from land_helpers import certified, child, fingerprint, git, harness, only_journal, recorded  # noqa: F401


def _attributes(root: Path, text: str) -> None:
    (root / ".git" / "info").mkdir(exist_ok=True)
    (root / ".git" / "info" / "attributes").write_text(text)


def _probe(root: Path, marker: Path) -> None:
    probe = f"sh -c 'touch {marker}; cat'"
    git(root, "config", "filter.probe.clean", probe)
    git(root, "config", "filter.probe.smudge", probe)
    git(root, "config", "filter.probe.required", "true")


def test_a_filter_on_an_affected_path_refuses_and_never_runs(harness: Path, tmp_path: Path, certified: None):
    marker = tmp_path / "filter-ran"
    _attributes(harness, "*.txt filter=probe\n")
    _probe(harness, marker)
    before = fingerprint(harness)
    with pytest.raises(AgentError, match="filter"):
        land.land(harness, "feature", strategy="ff")
    assert fingerprint(harness) == before and not marker.exists()


def test_recovery_over_a_filtered_repo_sees_the_edit_and_runs_no_filter(harness: Path, tmp_path: Path):
    recorded(harness)
    assert child(harness, "files_applied:exit").wait(60) == 97
    marker = tmp_path / "filter-ran"
    _attributes(harness, "*.txt filter=probe\n")
    _probe(harness, marker)
    (harness / "a.txt").write_text("an edit the clean filter would have hidden\n")
    before = fingerprint(harness)
    with pytest.raises(AgentError, match="changed after the landing began"):
        land_recover.recover(harness, only_journal(harness)["id"])
    assert fingerprint(harness) == before and not marker.exists()


@pytest.mark.parametrize("attribute", ["a.txt ident", "a.txt working-tree-encoding=UTF-16", "a.txt eol=crlf"])
def test_attributes_that_change_checked_out_bytes_refuse(harness: Path, attribute: str, certified: None):
    _attributes(harness, attribute + "\n")
    before = fingerprint(harness)
    with pytest.raises(AgentError, match="cannot move main safely"):
        land.land(harness, "feature", strategy="ff")
    assert fingerprint(harness) == before


@pytest.mark.parametrize(
    "key,value",
    [
        ("core.autocrlf", "true"),
        ("core.autocrlf", "input"),
        ("core.attributesFile", "/dev/null"),
        ("core.splitIndex", "true"),
    ],
)
def test_configurations_that_hide_or_change_bytes_refuse(
    harness: Path, key: str, value: str, certified: None
):
    git(harness, "config", key, value)
    with pytest.raises(AgentError, match="cannot move main safely"):
        land.land(harness, "feature", strategy="ff")
    assert git(harness, "rev-parse", "main") != git(harness, "rev-parse", "feature")


def test_autocrlf_spares_a_path_with_an_eol_attribute(harness: Path, certified: None):
    git(harness, "config", "core.autocrlf", "true")
    _attributes(harness, "*.txt eol=lf\n")
    assert land.land(harness, "feature", strategy="ff")["state"] == "done"


def test_an_empty_repo_autocrlf_wins_over_the_windows_default(
    harness: Path, monkeypatch: pytest.MonkeyPatch, certified: None
):
    """Git reads `core.autocrlf=` as false; a repo that sets the key, to any value, decides it."""
    monkeypatch.setattr(obs, "ON_WINDOWS", True)
    git(harness, "config", "core.autocrlf", "")
    assert land.land(harness, "feature", strategy="ff")["state"] == "done"


def test_a_global_autocrlf_never_overrides_the_windows_default(
    harness: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, certified: None
):
    """With the repo silent, a global `core.autocrlf=false` must not hide the CRLF working files
    Git for Windows' default `true` would leave over LF blobs."""
    monkeypatch.setattr(obs, "ON_WINDOWS", True)
    git(harness, "config", "--unset", "core.autocrlf")
    (tmp_path / "global").write_bytes(b"[core]\n\tautocrlf = false\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "global"))
    (harness / "a.txt").write_bytes(b"a1\r\n")
    with pytest.raises(AgentError, match="has no `eol` attribute, so core.autocrlf=true"):
        land.land(harness, "feature", strategy="ff")
    assert git(harness, "rev-parse", "main") != git(harness, "rev-parse", "feature")


@pytest.mark.parametrize("key", ["core.fileMode", "core.symlinks"])
def test_a_checkout_that_cannot_show_modes_lands_content(harness: Path, key: str, certified: None):
    git(harness, "config", key, "false")
    assert land.land(harness, "feature", strategy="ff")["state"] == "done"


@pytest.mark.parametrize("key,mode", [("core.fileMode", "100755"), ("core.symlinks", "120000")])
def test_a_change_the_checkout_cannot_show_refuses_by_name(harness: Path, key: str, mode: str):
    """Same content, another mode: a checkout that hides the mode cannot tell old from candidate."""
    git(harness, "config", key, "false")
    blob = git(harness, "rev-parse", "main:b.txt")
    git(harness, "switch", "-q", "-c", "hidden", "main")
    git(harness, "update-index", "--cacheinfo", f"{mode},{blob},b.txt")
    git(harness, "commit", "-q", "-m", "hidden")
    git(harness, "switch", "-q", "-f", "main")
    before = fingerprint(harness)
    with pytest.raises(AgentError, match=f"b.txt changes only from 100644 to {mode}, which {key}=false"):
        land.land(harness, "hidden", strategy="ff")
    assert fingerprint(harness) == before


def test_one_config_read_answers_get_and_bool_the_way_git_does(tmp_path: Path):
    git(tmp_path, "init", "-q")
    config = tmp_path / ".git" / "config"
    base = config.read_text()
    for value in ("yes", "On", "0", "1", "10", "false", "", "0x0", None):
        line = "\tfilemode\n" if value is None else f"\tfilemode = {value}\n\teol = {value}\n"
        config.write_text(f"{base}[core]\n\tfilemode = true\n{line}")
        settings = obs.Git(tmp_path).settings()
        want = git(tmp_path, "config", "--type=bool", "--get", "core.filemode", check=False)
        assert settings.flag("core.filemode") == want, value
        assert settings.get("core.eol") == git(tmp_path, "config", "--get", "core.eol", check=False)
    # Git for Windows' init writes symlinks = false; elsewhere the key is unset.
    symlinks = git(tmp_path, "config", "--type=bool", "--get", "core.symlinks", check=False)
    assert settings.flag("core.symlinks") == symlinks
    assert settings.flag("core.sparsecheckout") == "" and settings.get("core.attributesfile") == ""


def _branch(root: Path, name: str, change) -> str:
    git(root, "switch", "-q", "-c", name, "main")
    change(root)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", name)
    git(root, "switch", "-q", "main")
    return name


def test_a_submodule_pointer_change_refuses(harness: Path):
    head = git(harness, "rev-parse", "HEAD")

    def gitlink(root: Path) -> None:
        git(root, "update-index", "--add", "--cacheinfo", f"160000,{head},vendor/sub")

    git(harness, "switch", "-q", "-c", "sub", "main")
    gitlink(harness)
    git(harness, "commit", "-q", "-m", "sub")
    git(harness, "switch", "-q", "main")
    with pytest.raises(AgentError, match="submodule"):
        land.land(harness, "sub", strategy="ff")


@pytest.mark.skipif(
    sys.platform == "win32", reason="Windows files have no executable bit (core.fileMode=false)"
)
def test_a_mode_only_change_lands_and_reads_by_the_exec_bit(harness: Path, certified: None):
    _branch(harness, "modes", lambda r: os.chmod(r / "b.txt", 0o755))
    assert land.land(harness, "modes", strategy="ff")["state"] == "done"
    assert os.stat(harness / "b.txt").st_mode & 0o111
    seen = obs.observed(harness, "b.txt", "sha1")
    assert seen is not None and seen.mode == "100755"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows symlinks need Developer Mode, and Git for Windows checks links out as files",
)
def test_a_symlink_retarget_and_a_type_change_land(harness: Path, certified: None):
    def link(root: Path) -> None:
        (root / "link").symlink_to("a.txt")

    _branch(harness, "link", link)
    land.land(harness, "link", strategy="ff")

    def retarget(root: Path) -> None:
        (root / "link").unlink()
        (root / "link").symlink_to("b.txt")
        (root / "b.txt").unlink()
        (root / "b.txt").symlink_to("a.txt")

    _branch(harness, "retarget", retarget)
    assert land.land(harness, "retarget", strategy="ff")["state"] == "done"
    assert os.readlink(harness / "link") == "b.txt" and os.readlink(harness / "b.txt") == "a.txt"


def test_a_directory_where_a_file_belongs_reads_as_other_and_refuses(harness: Path, certified: None):
    (harness / "a.txt").unlink()
    (harness / "a.txt").mkdir()
    old_e, cand_e = land.entries(obs.Git(harness), {"old": "main", "cand": "feature"})
    assert obs.classify(harness, ["a.txt"], old_e, cand_e, "sha1") == {"a.txt": obs.OTHER}
    with pytest.raises(AgentError, match="differ from main"):
        land.land(harness, "feature", strategy="ff")
    assert (harness / "a.txt").is_dir() and not jn.lock_file(harness).exists()


def test_the_cli_lands_recovers_and_names_what_it_needs(harness: Path, run):
    assert "name the branch" in run(["land"]).err
    landed = run(["--json", "land", "feature", "--strategy", "ff"])
    assert landed.code == 0, landed.err
    journal = __import__("json").loads(landed.out)
    assert journal["state"] == "done"
    again = run(["--json", "land", "--recover", journal["id"]])
    assert again.code == 0 and __import__("json").loads(again.out)["state"] == "done"


def test_attributes_the_candidate_brings_are_checked_too(harness: Path):
    """Codex review: a candidate adding `a.txt eol=crlf` landed CRLF bytes and reported done."""

    def crlf(root: Path) -> None:
        (root / ".gitattributes").write_text("a.txt eol=crlf\n")
        (root / "a.txt").write_text("a3\n")

    _branch(harness, "crlf", crlf)
    before = fingerprint(harness)
    with pytest.raises(AgentError, match="eol: crlf"):
        land.land(harness, "crlf", strategy="ff")
    assert fingerprint(harness) == before and (harness / "a.txt").read_bytes() == b"a1\n"


def test_a_skip_worktree_entry_refuses(harness: Path, certified: None):
    git(harness, "update-index", "--skip-worktree", "a.txt")
    with pytest.raises(AgentError, match="skip-worktree"):
        land.land(harness, "feature", strategy="ff")


def test_a_file_edited_during_the_landing_keeps_it_unfinished_until_restored(harness: Path):
    """Main and the index reach the candidate, but the landing is not `done` while a file it changed
    differs; the edit is never overwritten, and recovery finishes once it is restored."""
    from land_helpers import paused, resume

    recorded(harness)
    proc = child(harness, "files_applied:pause")
    ident = paused(harness, proc)
    (harness / "a.txt").write_text("an edit made mid-landing\n")
    resume(harness, ident)
    assert proc.wait(60) == 3
    journal = jn.read(harness, ident)
    assert journal["state"] == "index_swapped" and journal["differs"] == ["a.txt"]
    assert (harness / "a.txt").read_text() == "an edit made mid-landing\n"
    # plan.md:86 requires trailers on main; the candidate's tree still matches the worker's tree.
    assert not jn.lock_file(harness).exists()
    assert git(harness, "rev-parse", "main") == journal["cand"]
    assert git(harness, "rev-parse", "main^{tree}") == git(harness, "rev-parse", "feature^{tree}")
    with pytest.raises(AgentError):
        land_recover.recover(harness, ident)
    git(harness, "checkout", "feature", "--", "a.txt")
    assert land_recover.recover(harness, ident)["state"] == "done"
