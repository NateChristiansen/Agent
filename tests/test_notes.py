"""`agent_cli.notes`: the private set (ADR-0012) and its four operations.

Everything runs under `tmp_path`. Nothing here creates a symlink, a file or a notes repo in
the real vault: the whole point of these operations is that they move the owner's data, so a
test that touched the vault would be the failure mode it is meant to catch.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from agent_cli import links, notes
from agent_cli.core import AgentError

EXAMPLES = {
    "agent.example.yaml": "version: 1\nrepos: []\n",
    ".mcp.example.json": '{"mcpServers": {}}\n',
    "harness/voice.example.md": "# Voice\n",
    "harness/owner.example/rules.md": "# Rules\n",
    "harness/owner.example/calibration.md": "# Calibration\n",
}

DIR_ENTRIES = ("features", "journal", "usage", "notes", "harness/owner")
# The generated FILES. `bases/` is generated too, but it is a directory, and a symlink to a
# directory that does not exist is one `atomic_write` cannot write through.
GENERATED = tuple(e.path for e in notes.PRIVATE_SET if e.generated and e.kind == "file")


@pytest.fixture
def harness(tmp_path: Path) -> Path:
    """A harness clone: `bin/agent` and the tracked examples, nothing else."""
    root = tmp_path / "Agent"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "agent").write_text("", encoding="utf-8")
    for rel, text in EXAMPLES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


@pytest.fixture
def notes_repo(tmp_path: Path) -> Path:
    root = tmp_path / "Agent-notes"
    root.mkdir()
    return root


def _file_symlink(at: Path, target: Path) -> None:
    try:
        at.symlink_to(target)
    except OSError:
        if sys.platform != "win32":
            raise
        pytest.skip("this Windows account cannot make file symlinks (no Developer Mode)")


def _states(rows: list[dict]) -> dict[str, str]:
    return {r["path"]: r["state"] for r in rows}


# ------------------------------------------------------------------ the registry itself


def test_hygiene_lines_are_restored_only_where_the_files_exist(tmp_path: Path):
    """Doctor's notes .gitignore/.gitattributes check was retired,
    and setup never updates an existing file. Sync now restores missing lines in place."""
    (tmp_path / ".gitignore").write_text(".agent/\nmine.tmp")  # no trailing newline, lines missing
    assert notes.ensure_hygiene(tmp_path) == [".gitignore"]
    lines = (tmp_path / ".gitignore").read_text().splitlines()
    assert lines[:2] == [".agent/", "mine.tmp"] and "bases/*.base" in lines and "decisions/README.md" in lines
    assert not (tmp_path / ".gitattributes").exists()  # never created here
    (tmp_path / ".gitattributes").write_text("")
    assert notes.ensure_hygiene(tmp_path) == [".gitattributes"]
    assert "features/*/log.md merge=union" in (tmp_path / ".gitattributes").read_text()
    assert notes.ensure_hygiene(tmp_path) == []


def test_the_private_set_is_one_registry_of_well_formed_entries():
    paths = [e.path for e in notes.PRIVATE_SET]
    assert len(paths) == len(set(paths)), "no path twice"
    for entry in notes.PRIVATE_SET:
        assert entry.kind in ("dir", "file"), entry
        assert not entry.path.startswith("/") and not entry.path.endswith("/"), entry
        if entry.generated:
            assert entry.example is None, "a generated file is written, never copied"
    # `adrs/` is public and tracked, README included. It must never creep into the set.
    assert not any(e.path.startswith("adrs") for e in notes.PRIVATE_SET)
    assert {e.path for e in notes.PRIVATE_SET} >= {"features", "agent.yaml", ".mcp.json", "bases"}


# ------------------------------------------------------------------ link


def test_link_into_a_fresh_notes_repo_links_every_entry(harness: Path, notes_repo: Path):
    rows = notes.link(harness, notes_repo)
    assert _states(rows) == {e.path: "done" for e in notes.PRIVATE_SET}
    for entry in notes.PRIVATE_SET:
        link = harness / entry.path
        if sys.platform == "win32":  # junctions and hard links, unless the account can symlink
            assert links.points_at(link, notes_repo / entry.path), entry.path
        else:
            assert link.is_symlink(), entry.path
            assert not os.path.isabs(os.readlink(link)), "relative, so a moved pair keeps working"
            assert notes._symlink_target(link) == notes_repo / entry.path
        # the target exists in the notes repo, except the generated files status render writes
        assert (notes_repo / entry.path).exists() is (entry.path not in GENERATED), entry.path
    assert (notes_repo / "bases").is_dir(), "a generated directory is created; only files wait"


def test_link_copies_the_example_into_the_notes_repo(harness: Path, notes_repo: Path):
    notes.link(harness, notes_repo)
    assert (notes_repo / "agent.yaml").read_text(encoding="utf-8") == EXAMPLES["agent.example.yaml"]
    assert (harness / "agent.yaml").read_text(encoding="utf-8") == EXAMPLES["agent.example.yaml"]
    assert (notes_repo / "harness" / "owner" / "rules.md").exists(), "a dir example is copied whole"
    assert (harness / "harness" / "owner" / "calibration.md").exists()


def test_link_is_idempotent(harness: Path, notes_repo: Path):
    notes.link(harness, notes_repo)
    rows = notes.link(harness, notes_repo)
    assert _states(rows) == {e.path: "already" for e in notes.PRIVATE_SET}


def test_link_refuses_a_real_directory_and_leaves_it_alone(harness: Path, notes_repo: Path):
    (harness / "features" / "001-x").mkdir(parents=True)
    (harness / "features" / "001-x" / "plan.md").write_text("mine\n", encoding="utf-8")
    rows = notes.link(harness, notes_repo)
    row = next(r for r in rows if r["path"] == "features")
    assert row["state"] == "refused"
    assert "real directory" in row["detail"] and str(notes_repo / "features") in row["detail"]
    assert (harness / "features" / "001-x" / "plan.md").read_text(encoding="utf-8") == "mine\n"
    assert not links.is_link(harness / "features")
    assert _states(rows)["journal"] == "done", "one refusal does not stop the others"


def test_link_refuses_a_real_file_and_leaves_it_alone(harness: Path, notes_repo: Path):
    (harness / "agent.yaml").write_text("version: 1\nrepos: [mine]\n", encoding="utf-8")
    row = next(r for r in notes.link(harness, notes_repo) if r["path"] == "agent.yaml")
    assert row["state"] == "refused"
    assert "real file" in row["detail"]
    assert (harness / "agent.yaml").read_text(encoding="utf-8") == "version: 1\nrepos: [mine]\n"


def test_link_refuses_a_symlink_that_points_somewhere_else(harness: Path, notes_repo: Path, tmp_path):
    elsewhere = tmp_path / "other-notes" / "journal"
    elsewhere.mkdir(parents=True)
    links.make(harness / "journal", elsewhere, is_dir=True)
    row = next(r for r in notes.link(harness, notes_repo) if r["path"] == "journal")
    assert row["state"] == "refused"
    assert str(elsewhere) in row["detail"], "it names where the link actually points"
    assert notes._symlink_target(harness / "journal") == elsewhere


def test_link_refuses_a_notes_root_that_is_not_a_directory(harness: Path, tmp_path: Path):
    with pytest.raises(AgentError) as exc:
        notes.link(harness, tmp_path / "nope")
    assert "notes init" in exc.value.message


def test_link_skips_an_entry_whose_example_this_clone_lacks(harness: Path, notes_repo: Path):
    (harness / "agent.example.yaml").unlink()
    row = next(r for r in notes.link(harness, notes_repo) if r["path"] == "agent.yaml")
    assert row["state"] == "skipped"
    assert "agent.example.yaml" in row["detail"]
    assert not (harness / "agent.yaml").exists(), "no empty registry left behind"


# ------------------------------------------------------------------ Windows links


@pytest.fixture
def no_symlinks(monkeypatch: pytest.MonkeyPatch):
    """Plain Windows without Developer Mode: symlinks are refused, so files fall back to hard
    links. Directories would be junctions, which need Windows; this covers the file path."""
    from agent_cli import links

    def refuse(*args, **kwargs):
        raise OSError("A required privilege is not held by the client")

    monkeypatch.setattr(links.os, "symlink", refuse)
    monkeypatch.setattr(links, "IS_WINDOWS", True)
    return links


def test_a_file_falls_back_to_a_hard_link_when_symlinks_are_refused(
    harness: Path, notes_repo: Path, no_symlinks
):
    (notes_repo / "agent.yaml").write_text("mine\n", encoding="utf-8")
    entry = next(e for e in notes.PRIVATE_SET if e.path == "agent.yaml")
    row = notes._link_one(entry, harness / "agent.yaml", notes_repo / "agent.yaml", harness)
    assert row["state"] == "done" and "hardlink" in row["detail"]
    assert no_symlinks.is_hard_linked(harness / "agent.yaml", notes_repo / "agent.yaml")
    (harness / "agent.yaml").write_text("edited in place\n", encoding="utf-8")
    assert (notes_repo / "agent.yaml").read_text(encoding="utf-8") == "edited in place\n", "one file"


def test_a_hard_link_across_drives_is_refused_with_the_reason(
    harness: Path, notes_repo: Path, no_symlinks, monkeypatch
):
    monkeypatch.setattr(no_symlinks, "_same_volume", lambda a, b: False)
    (notes_repo / "agent.yaml").write_text("mine\n", encoding="utf-8")
    entry = next(e for e in notes.PRIVATE_SET if e.path == "agent.yaml")
    row = notes._link_one(entry, harness / "agent.yaml", notes_repo / "agent.yaml", harness)
    assert row["state"] == "refused"
    assert "another drive" in row["detail"]
    assert not (harness / "agent.yaml").exists()


def test_a_symlink_to_another_drive_is_absolute(tmp_path: Path, monkeypatch):
    """Windows `relpath` raises across drives (the CI runner's checkout is on D:, its temp on
    C:). That must fall through to an absolute symlink, not crash before the fallbacks run."""
    from agent_cli import links

    def other_drive(target, start):
        raise ValueError("path is on mount 'D:', start on mount 'C:'")

    made = []
    monkeypatch.setattr(links.os.path, "relpath", other_drive)
    # Captured, not real: a Windows job without symlink privilege would make a junction instead,
    # and this test is about the target handed to os.symlink, not the account's privileges.
    monkeypatch.setattr(links.os, "symlink", lambda src, dst, **kw: made.append((src, dst, kw)))
    target = tmp_path / "notes" / "journal"
    target.mkdir(parents=True)
    path = tmp_path / "harness" / "journal"
    assert links.make(path, target, is_dir=True) == "symlink"
    assert made == [(str(target.resolve()), path, {"target_is_directory": True})]


def _commit(repo: Path, rel: str, text: str) -> None:
    import subprocess

    (repo / rel).write_text(text, encoding="utf-8")
    for args in (
        ["init", "-q"],
        ["add", rel],
        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", rel],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_a_diverged_hard_link_holding_an_edit_is_refused_with_both_copies_kept(
    harness: Path, notes_repo: Path
):
    """An atomic replace in the harness severs a hard link and leaves an edit the notes repo
    never committed. Setup refuses it and moves nothing; neither copy wins."""
    import os

    _commit(notes_repo, "agent.yaml", "notes copy\n")
    os.link(notes_repo / "agent.yaml", harness / "agent.yaml")
    notes.write_notes_root(harness, notes_repo)
    # the per-file provenance `link` records when it links a file
    notes.write_linked_record(harness, {"agent.yaml": str(notes_repo / "agent.yaml")})
    replacement = harness / "agent.yaml.tmp"
    replacement.write_text("harness copy\n", encoding="utf-8")
    os.replace(replacement, harness / "agent.yaml")  # the atomic replace that severs the link

    states = {r["path"]: r["state"] for r in notes.status(harness)}
    assert states["agent.yaml"] == "diverged"

    entry = next(e for e in notes.PRIVATE_SET if e.path == "agent.yaml")
    row = notes._link_one(entry, harness / "agent.yaml", notes_repo / "agent.yaml", harness, True, notes_repo)
    assert row["state"] == "refused" and "holds an edit" in row["detail"]
    assert not (harness / ".agent" / "scratch" / "notes-diverged").exists()
    assert (harness / "agent.yaml").read_text(encoding="utf-8") == "harness copy\n"
    assert (notes_repo / "agent.yaml").read_text(encoding="utf-8") == "notes copy\n"
    assert {r["path"]: r["state"] for r in notes.status(harness)}["agent.yaml"] == "diverged"


def test_a_stale_diverged_hard_link_is_recovered_without_deleting_anything(harness: Path, notes_repo: Path):
    """Git replaced the notes side, so the harness keeps a copy the notes repo committed. The
    current notes copy wins; the stale one is parked under the ignored
    .agent/scratch/notes-diverged/, and nothing new lands in the checkout."""
    import os

    _commit(notes_repo, "agent.yaml", "first\n")
    os.link(notes_repo / "agent.yaml", harness / "agent.yaml")
    notes.write_linked_record(harness, {"agent.yaml": str(notes_repo / "agent.yaml")})
    stale = (harness / "agent.yaml").read_bytes()
    replacement = notes_repo / "agent.yaml.tmp"
    replacement.write_text("notes copy\n", encoding="utf-8")
    os.replace(replacement, notes_repo / "agent.yaml")  # what a pull does to the notes side

    rows = {r["path"]: r for r in notes.link(harness, notes_repo)}
    assert rows["agent.yaml"]["state"] == "done" and "previous copy" in rows["agent.yaml"]["detail"]
    parked = list((harness / ".agent" / "scratch" / "notes-diverged").iterdir())
    assert len(parked) == 1 and parked[0].read_bytes() == stale
    assert (harness / "agent.yaml").read_text(encoding="utf-8") == "notes copy\n"
    notes.write_notes_root(harness, notes_repo)
    assert {r["path"]: r["state"] for r in notes.status(harness)}["agent.yaml"] == "linked"


@pytest.fixture
def hard_linked(harness: Path, notes_repo: Path) -> Path:
    """`agent.yaml` hard-linked into the notes repo, as plain Windows links it."""
    if not hasattr(os, "link"):
        pytest.skip("no os.link on this platform")
    (notes_repo / "agent.yaml").write_text("version: 1\nrepos: []\n", encoding="utf-8")
    os.link(notes_repo / "agent.yaml", harness / "agent.yaml")
    notes.write_notes_root(harness, notes_repo)
    notes.write_linked_record(harness, {"agent.yaml": str(notes_repo / "agent.yaml")})
    return harness / "agent.yaml"


def test_registering_a_repo_through_notes_write_keeps_the_hard_link(
    harness: Path, notes_repo: Path, hard_linked: Path
):
    """The agent-config S6 route on a hard-linked install (alpha F1): draft, edit the draft,
    preview, apply. Editing `agent.yaml` itself with Edit or apply_patch severed the link."""
    draft = Path(notes.write_file(harness, target="agent.yaml")["draft"])
    replacement = draft.with_name("edit.tmp")  # the Edit tool's own atomic replace, on the draft
    replacement.write_text("version: 1\nrepos:\n- name: svc\n  short: svc\n", encoding="utf-8")
    os.replace(replacement, draft)

    preview = notes.write_file(harness, target="agent.yaml", source=draft)
    assert not preview["applied"] and "+- name: svc" in preview["diff"]
    applied = notes.write_file(
        harness, target="agent.yaml", source=draft, apply=True, expect_sha256=preview["before_sha256"]
    )

    assert applied["applied"] and applied["link"] == "linked"
    assert os.path.samefile(hard_linked, notes_repo / "agent.yaml")
    assert (notes_repo / "agent.yaml").read_text(encoding="utf-8") == draft.read_text(encoding="utf-8")
    assert "svc" in hard_linked.read_text(encoding="utf-8")
    assert os.stat(hard_linked).st_nlink == 2
    assert _states(notes.status(harness))["agent.yaml"] == "linked"


def test_notes_write_keeps_the_main_checkout_and_a_manager_worktree_linked(
    harness: Path, notes_repo: Path, hard_linked: Path, tmp_path: Path
):
    """Plain Windows links the private set into each manager worktree too, so the file has three
    names: notes, main checkout, worktree. A write through the worktree keeps all three."""
    import subprocess

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(harness), "-c", "user.name=t", "-c", "user.email=t@t", *args],
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    git("add", "bin")
    git("commit", "-qm", "init")
    worktree = tmp_path / "Agent-worktrees" / "mgr"
    git("worktree", "add", "-q", "-b", "mgr", str(worktree))
    os.link(notes_repo / "agent.yaml", worktree / "agent.yaml")
    notes.write_notes_root(worktree, notes_repo)
    assert os.stat(hard_linked).st_nlink == 3

    draft = Path(notes.write_file(worktree, target="agent.yaml")["draft"])
    draft.write_text("version: 1\nrepos:\n- name: svc\n  short: svc\n", encoding="utf-8")
    preview = notes.write_file(worktree, target="agent.yaml", source=draft)
    applied = notes.write_file(
        worktree, target="agent.yaml", source=draft, apply=True, expect_sha256=preview["before_sha256"]
    )

    assert applied["applied"] and applied["link"] == "linked"
    copy = notes_repo / "agent.yaml"
    assert os.path.samefile(copy, hard_linked) and os.path.samefile(copy, worktree / "agent.yaml")
    assert os.stat(copy).st_nlink == 3
    assert "svc" in copy.read_text(encoding="utf-8")
    assert _states(notes.status(harness))["agent.yaml"] == "linked"


def test_notes_write_refuses_a_file_changed_since_the_preview(harness: Path, hard_linked: Path):
    draft = Path(notes.write_file(harness, target="agent.yaml")["draft"])
    draft.write_text("version: 1\nrepos: []\n# mine\n", encoding="utf-8")
    preview = notes.write_file(harness, target="agent.yaml", source=draft)
    hard_linked.write_text("version: 1\nrepos: []\n# theirs\n", encoding="utf-8")
    with pytest.raises(AgentError, match="changed since the preview"):
        notes.write_file(
            harness, target="agent.yaml", source=draft, apply=True, expect_sha256=preview["before_sha256"]
        )
    assert hard_linked.read_text(encoding="utf-8").endswith("# theirs\n")


def test_notes_write_only_takes_a_private_file(harness: Path, hard_linked: Path):
    with pytest.raises(AgentError, match="target must be one of"):
        notes.write_file(harness, target="agent.local.yaml")


def test_atomic_write_keeps_a_hard_linked_private_file_linked(
    harness: Path, notes_repo: Path, hard_linked: Path
):
    from agent_cli.core import atomic_write

    atomic_write(hard_linked, "version: 1\nrepos: []\n# written\n")
    assert os.path.samefile(hard_linked, notes_repo / "agent.yaml")
    assert (notes_repo / "agent.yaml").read_text(encoding="utf-8").endswith("# written\n")
    assert not [p for p in notes_repo.iterdir() if p.name.startswith(".agent.yaml.")], "no side file left"
    assert _states(notes.status(harness))["agent.yaml"] == "linked"


def test_atomic_write_refuses_to_sever_a_hard_link_it_cannot_account_for(tmp_path: Path):
    if not hasattr(os, "link"):
        pytest.skip("no os.link on this platform")
    from agent_cli.core import atomic_write

    here = tmp_path / "pair"
    here.mkdir()
    a, b = here / "a.txt", here / "b.txt"
    a.write_text("old\n", encoding="utf-8")
    os.link(a, b)
    with pytest.raises(AgentError, match="would sever"):
        atomic_write(a, "new\n")
    assert os.path.samefile(a, b) and b.read_text(encoding="utf-8") == "old\n"
    assert sorted(p.name for p in here.iterdir()) == ["a.txt", "b.txt"]


def test_a_refused_plain_file_is_never_recovered_on_a_later_run(harness: Path, notes_repo: Path):
    """A first link refuses an arbitrary plain file. A second run must not then treat it as a
    diverged link and move it: only files this clone actually linked are recoverable."""
    (notes_repo / "agent.yaml").write_text("notes copy\n", encoding="utf-8")
    (harness / "agent.yaml").write_text("mine, never linked\n", encoding="utf-8")
    for _ in range(2):
        rows = {r["path"]: r for r in notes.link(harness, notes_repo)}
        notes.write_notes_root(harness, notes_repo)
        assert rows["agent.yaml"]["state"] == "refused"
    assert (harness / "agent.yaml").read_text(encoding="utf-8") == "mine, never linked\n"
    assert not (harness / ".agent" / "scratch" / "notes-diverged").exists()
    assert {r["path"]: r["state"] for r in notes.status(harness)}["agent.yaml"] == "plain"


def test_recoveries_in_the_same_second_never_overwrite_a_parked_copy(
    harness: Path, notes_repo: Path, monkeypatch: pytest.MonkeyPatch
):
    from agent_cli import links

    monkeypatch.setattr(links.time, "strftime", lambda fmt: "20260925-120000")  # a frozen clock
    (notes_repo / "agent.yaml").write_text("notes copy\n", encoding="utf-8")
    parked = []
    for n in range(3):
        (harness / "agent.yaml").unlink(missing_ok=True)
        (harness / "agent.yaml").write_text(f"copy {n}\n", encoding="utf-8")
        stale = (harness / "agent.yaml").read_bytes()
        parked.append(
            links.recover(harness, harness / "agent.yaml", notes_repo / "agent.yaml", expected=stale)
        )
    assert len(set(parked)) == 3
    assert [p.read_text(encoding="utf-8") for p in parked] == ["copy 0\n", "copy 1\n", "copy 2\n"]


def test_a_plain_file_is_not_recovered_when_the_repo_was_never_linked_here(harness: Path, notes_repo: Path):
    """Recovery is only for a link that diverged. A plain file in a clone never linked to this
    repo is the owner's to move, so it is refused and kept."""
    (notes_repo / "agent.yaml").write_text("notes copy\n", encoding="utf-8")
    (harness / "agent.yaml").write_text("mine\n", encoding="utf-8")
    entry = next(e for e in notes.PRIVATE_SET if e.path == "agent.yaml")
    row = notes._link_one(entry, harness / "agent.yaml", notes_repo / "agent.yaml", harness, False)
    assert row["state"] == "refused"
    assert (harness / "agent.yaml").read_text(encoding="utf-8") == "mine\n"


# ------------------------------------------------------------------ init


def test_init_builds_a_skeleton_that_is_a_git_repo(harness: Path, tmp_path: Path):
    dest = tmp_path / "fresh-notes"
    rows = notes.init(dest, root=harness)
    states = _states(rows)
    for path in DIR_ENTRIES:
        assert states[path] == "done" and (dest / path).is_dir(), path
    assert (dest / "agent.yaml").read_text(encoding="utf-8") == EXAMPLES["agent.example.yaml"]
    for path in GENERATED:
        assert states[path] == "skipped" and not (dest / path).exists(), path
    assert (dest / "bases").is_dir()
    readme = (dest / "README.md").read_text(encoding="utf-8")
    assert notes.README_MARKER in readme
    assert f"--notes {dest}" in readme
    assert "features" in readme
    assert (dest / ".gitignore").read_text(encoding="utf-8") == notes.GITIGNORE
    assert (dest / ".gitattributes").read_text(encoding="utf-8") == notes.GITATTRIBUTES
    assert (dest / ".git").is_dir()
    assert states["git"] == "done"
    top = {e.path.split("/")[0] for e in notes.PRIVATE_SET}
    extra = {"README.md", ".gitignore", ".gitattributes", ".git", "bases"}
    assert {p.name for p in dest.iterdir()} <= top | extra, "init writes nothing outside the set"


def test_init_ignores_what_a_render_regenerates(harness: Path, tmp_path: Path):
    """ADR-0034: two machines both render these, so tracked they conflict on push. A decision
    record beside the generated index is the owner's data and stays visible to git."""
    import subprocess

    dest = tmp_path / "fresh-notes"
    notes.init(dest, root=harness)

    def ignored(rel: str) -> bool:
        proc = subprocess.run(["git", "check-ignore", "-q", rel], cwd=dest, check=False)
        return proc.returncode == 0

    assert ignored("bases/features.base")
    assert ignored("decisions/README.md")
    assert not ignored("decisions/0001-work.md")
    assert not ignored("bases/.gitkeep")


def test_a_clone_of_a_fresh_notes_repo_keeps_every_link_target(harness: Path, tmp_path: Path):
    """With the generated files ignored, nothing tracked kept `bases/` or `decisions/` alive: a
    clone lacked them, and the link into a missing directory broke the next render."""
    import subprocess

    def git(*args: str, cwd: Path) -> None:
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
        )

    dest = tmp_path / "fresh-notes"
    notes.init(dest, root=harness)
    (dest / "bases" / "features.base").write_text("generated\n", encoding="utf-8")
    (dest / "decisions" / "README.md").write_text("generated\n", encoding="utf-8")
    (dest / "decisions" / "0001-work.md").write_text("a record\n", encoding="utf-8")
    git("add", "-A", cwd=dest)
    git("commit", "-q", "-m", "first", cwd=dest)
    git("clone", "-q", str(dest), str(tmp_path / "clone"), cwd=tmp_path)

    clone = tmp_path / "clone"
    for entry in notes.PRIVATE_SET:
        if entry.kind == "dir":
            assert (clone / entry.path).is_dir(), entry.path
    assert (clone / "decisions" / "0001-work.md").is_file()
    assert not (clone / "decisions" / "README.md").exists()
    assert not (clone / "bases" / "features.base").exists()


def test_init_makes_no_commit(harness: Path, tmp_path: Path):
    import subprocess

    dest = tmp_path / "fresh-notes"
    notes.init(dest, root=harness)
    proc = subprocess.run(["git", "log", "--oneline"], cwd=dest, capture_output=True, text=True)
    assert proc.returncode != 0, "no commit yet; the first one is the owner's"


def test_init_is_idempotent(harness: Path, tmp_path: Path):
    dest = tmp_path / "fresh-notes"
    notes.init(dest, root=harness)
    (dest / "features" / "001-x").mkdir(parents=True)
    rows = notes.init(dest, root=harness)
    assert set(_states(rows).values()) <= {"already", "skipped"}
    assert (dest / "features" / "001-x").is_dir(), "a second run leaves what is there"


def test_init_refuses_a_non_empty_foreign_directory(harness: Path, tmp_path: Path):
    dest = tmp_path / "someone-elses"
    dest.mkdir()
    (dest / "important.txt").write_text("do not touch\n", encoding="utf-8")
    with pytest.raises(AgentError) as exc:
        notes.init(dest, root=harness)
    assert "not empty" in exc.value.message
    assert (dest / "important.txt").exists()
    assert not (dest / "features").exists()


def test_init_accepts_a_directory_that_only_holds_a_git_dir(harness: Path, tmp_path: Path):
    import subprocess

    dest = tmp_path / "git-first"
    dest.mkdir()
    subprocess.run(["git", "init", "-q", str(dest)], check=True, capture_output=True)
    rows = notes.init(dest, root=harness)
    assert _states(rows)["git"] == "already"
    assert (dest / "features").is_dir()


# ------------------------------------------------------------------ status


def test_status_reports_each_state(harness: Path, notes_repo: Path):
    notes.link(harness, notes_repo)  # every entry linked; generated targets not written yet
    (notes_repo / "features" / ".gitkeep").unlink()
    (notes_repo / "features").rmdir()  # the notes repo lost one: a broken link
    (harness / "agent.yaml").unlink()
    (harness / "agent.yaml").write_text("replaced\n", encoding="utf-8")  # a linked file, now a copy
    (harness / "bases").unlink()
    (harness / "bases").mkdir()  # a real directory where a link belongs: plain
    (harness / "journal").unlink()

    rows = {r["path"]: r for r in notes.status(harness)}
    assert rows["notes"]["state"] == "linked"
    assert rows["notes"]["target"] == str(notes_repo / "notes")
    assert rows["features"]["state"] == "broken"
    assert rows["features"]["target"] == str(notes_repo / "features")
    assert rows["agent.yaml"]["state"] == "diverged"
    assert rows["bases"]["state"] == "plain"
    assert rows["journal"]["state"] == "missing"
    assert rows["journal"]["target"] == ""
    assert "STATUS.md" not in rows, "a plain generated file in every clone"


def test_status_calls_real_private_directories_plain(harness: Path):
    (harness / "features").mkdir()
    (harness / "bases").mkdir()
    states = {r["path"]: r["state"] for r in notes.status(harness)}
    assert states["features"] == "plain"
    assert states["bases"] == "plain"


def test_root_generated_notes_are_never_linked_and_old_links_are_removed(
    harness: Path, notes_repo: Path, run
):
    """Found live: Obsidian watches a symlinked directory but not a symlinked file
    at the vault root, so it never saw a render land in Dashboard.md and saved its stale copy
    back over it. Those files are plain in every clone, and a render replaces an old link."""
    assert not set(notes.LOCAL_GENERATED_FILES) & {e.path for e in notes.PRIVATE_SET}
    (notes_repo / "Dashboard.md").write_text("old layout\n", encoding="utf-8")
    _file_symlink(harness / "Dashboard.md", notes_repo / "Dashboard.md")
    (harness / "Index.md").write_text("plain already\n", encoding="utf-8")
    rows = notes.unlink_local_generated(harness)
    assert [(r["path"], r["state"]) for r in rows] == [("Dashboard.md", "done")]
    assert not (harness / "Dashboard.md").exists(), "the link is gone; render writes the file"
    assert (notes_repo / "Dashboard.md").read_text(encoding="utf-8") == "old layout\n", (
        "the target is left alone"
    )
    assert (harness / "Index.md").read_text(encoding="utf-8") == "plain already\n"


def test_status_render_writes_a_plain_dashboard_over_an_old_link(tmp_root: Path, tmp_path: Path, run):
    old = tmp_path / "notes-copy.md"
    old.write_text("old layout\n", encoding="utf-8")
    _file_symlink(tmp_root / "Dashboard.md", old)
    run(["status", "render"])
    assert not (tmp_root / "Dashboard.md").is_symlink()
    # Plan B2 moves Open features/tasks into the pane and removes their Dataview blocks.
    text = (tmp_root / "Dashboard.md").read_text(encoding="utf-8")
    assert "```agent-live\n```" in text
    assert 'views/features"' not in text and 'views/tasks"' not in text
    assert old.read_text(encoding="utf-8") == "old layout\n"


# ------------------------------------------------------------------ the notes-root memory


def test_notes_root_round_trips_and_resolves_relative(harness: Path, notes_repo: Path):
    assert notes.read_notes_root(harness) is None
    notes.write_notes_root(harness, notes_repo)
    assert notes.read_notes_root(harness) == notes_repo
    notes.write_notes_root(harness, "../Agent-notes")
    assert notes.read_notes_root(harness) == harness / "../Agent-notes"
    assert (harness / notes.NOTES_ROOT_FILE).read_text(encoding="utf-8") == "../Agent-notes\n"
