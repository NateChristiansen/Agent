import os
import shutil
import stat
from pathlib import Path

import pytest

from conftest import git


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, [], {}])
def test_config_network_switch_refuses_non_boolean_at_load(tmp_root: Path, value):
    import yaml

    from agent_cli import repos
    from agent_cli.core import AgentError

    path = tmp_root / "agent.yaml"
    data = yaml.safe_load(path.read_text())
    data["repos"][0].setdefault("dispatch", {})["codex_network_access"] = value
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(AgentError, match=r"dispatch\.codex_network_access must be a boolean"):
        repos.load_registry(tmp_root)


def test_list(tmp_root: Path, run):
    r = run(["--json", "repos", "list"])
    assert [x["short"] for x in r.json] == ["alpha", "beta"]
    assert r.json[0]["exists"] is True
    assert r.json[1]["exists"] is False


def test_status_reports_branch_and_dirty(tmp_root: Path, run):
    alpha = tmp_root / "repos/alpha-api"
    (alpha / "new.txt").write_text("x")
    r = run(["--json", "repos", "status", "alpha"])
    info = r.json[0]
    assert info["branch"] == "main"
    assert info["dirty"] == 1
    assert info["ahead"] == 0 and info["behind"] == 0
    assert info["remote_ok"] is True
    assert run(["repos", "status", "nope"]).code == 1


def test_sync_no_clone_and_clone(tmp_root: Path, run):
    r = run(["--json", "repos", "sync", "beta", "--no-clone"])
    assert r.code == 0
    assert r.json[0]["actions"] == ["missing (not cloned; --no-clone)"]
    # point beta at the alpha bare remote so a clone can succeed offline
    reg = tmp_root / "agent.yaml"
    text = reg.read_text().replace(
        "git@github.com:example/beta-web.git", str(tmp_root.parent / "alpha-remote.git")
    )
    reg.write_text(text)
    r = run(["--json", "repos", "sync", "beta"])
    assert r.json[0]["actions"] == ["cloned"]
    assert (tmp_root / "repos/beta-web/.git").exists()


def test_sync_repairs_worktrees(tmp_root: Path, run):
    alpha = tmp_root / "repos/alpha-api"
    wt_dir = tmp_root / "repos/_worktrees/alpha"
    wt_dir.mkdir(parents=True)
    git(["worktree", "add", "-q", "-b", "feature/x", str(wt_dir / "x")], alpha)
    r = run(["--json", "repos", "sync", "alpha"])
    assert "worktree repair (1)" in r.json[0]["actions"]
    assert [w["branch"] for w in r.json[0]["worktrees"]] == ["feature/x"]


def test_sync_repairs_worktree_through_native_case_alias(tmp_root: Path, run):
    alpha = tmp_root / "repos/alpha-api"
    wt_dir = tmp_root / "repos/_worktrees/alpha"
    wt_dir.mkdir(parents=True)
    git(["worktree", "add", "-q", "-b", "feature/case", str(wt_dir / "case")], alpha)
    case_alias = tmp_root / "repos/_WORKTREES/alpha"
    if not case_alias.is_dir():
        pytest.skip("filesystem distinguishes _worktrees from _WORKTREES")
    assert case_alias.samefile(wt_dir)

    registry = tmp_root / "agent.yaml"
    registry.write_text(registry.read_text().replace("repos/_worktrees/alpha", "repos/_WORKTREES/alpha"))
    result = run(["--json", "repos", "sync", "alpha"])

    assert result.code == 0, result.err
    assert "worktree repair (1)" in result.json[0]["actions"]

    registry.write_text(registry.read_text().replace("repos/_WORKTREES/alpha", "repos/_worktrees/alpha/case"))
    result = run(["--json", "repos", "sync", "alpha"])

    assert result.code == 0, result.err
    assert not any(action.startswith("worktree repair") for action in result.json[0]["actions"])


def _native(path: str | Path) -> str:
    """A path's identity on this platform: resolved, under the platform's case rules."""
    return os.path.normcase(str(Path(path).resolve()))


def _registrations(repo: Path) -> dict[str, dict[str, str]]:
    """Git's worktree registrations by native identity; porcelain prints `C:/...` on Windows."""
    out: dict[str, dict[str, str]] = {}
    current: dict[str, str] = {}
    for line in git(["worktree", "list", "--porcelain"], repo).splitlines():
        key, _, value = line.partition(" ")
        if key == "worktree":
            current = out.setdefault(_native(value), {})
        elif key:
            current[key] = value
    return out


def _overwrite(path: Path, data: bytes) -> None:
    """Replace a file's bytes in place. Git for Windows marks a worktree's `.git` file hidden, and
    Windows refuses to create over a hidden file, so `write_text` raises PermissionError there."""
    with path.open("r+b") as fh:
        fh.write(data)
        fh.truncate()


def _hidden(path: Path) -> bool:
    return bool(getattr(path.stat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_HIDDEN)


def test_sync_skips_deleted_registered_worktree_without_pruning(tmp_root: Path, run):
    alpha = tmp_root / "repos/alpha-api"
    wt_dir = tmp_root / "repos/_worktrees/alpha"
    deleted = wt_dir / "deleted"
    wt_dir.mkdir(parents=True)
    git(["worktree", "add", "-q", "-b", "feature/deleted", str(deleted)], alpha)
    shutil.rmtree(deleted)
    registration_before = _registrations(alpha)[_native(deleted)]
    assert "prunable" in registration_before
    admin = alpha / ".git/worktrees/deleted"
    admin_before = {p.name: p.read_bytes() for p in admin.iterdir() if p.is_file()}
    _advance_remote(tmp_root)
    remote_head = git(["rev-parse", "refs/heads/main"], tmp_root.parent / "alpha-remote.git").strip()

    result = run(["--json", "repos", "sync", "alpha", "--pull"])

    assert result.code == 0, result.err
    assert git(["rev-parse", "origin/main"], alpha).strip() == remote_head
    assert git(["rev-parse", "HEAD"], alpha).strip() == remote_head
    assert _registrations(alpha)[_native(deleted)] == registration_before
    assert {p.name: p.read_bytes() for p in admin.iterdir() if p.is_file()} == admin_before


def test_sync_registration_discovery_failure_stops_before_fetch(tmp_root: Path, run, monkeypatch):
    from agent_cli import repos as repos_mod

    wt_dir = tmp_root / "repos/_worktrees/alpha"
    wt_dir.mkdir(parents=True)
    commands: list[list[str]] = []
    real_run_git = repos_mod.run_git

    def fail_discovery(args: list[str], cwd: Path) -> str:
        commands.append(args)
        if args == ["worktree", "list", "--porcelain"]:
            raise repos_mod.ExternalFailure("fixture discovery failure")
        return real_run_git(args, cwd)

    monkeypatch.setattr(repos_mod, "run_git", fail_discovery)
    result = run(["--json", "repos", "sync", "alpha", "--pull"])

    assert result.code != 0
    assert "worktree registration discovery failed" in result.err
    assert "fixture discovery failure" in result.err
    assert "fetch" not in [arg for command in commands for arg in command]
    assert not any(command[:1] == ["merge"] for command in commands)


@pytest.mark.parametrize("args", [[], ["--fetch"], ["--pull"]], ids=["plain", "fetch-only", "pull"])
def test_sync_ignores_unregistered_directories_and_preserves_worktrees(
    tmp_root: Path, run, args, monkeypatch
):
    from agent_cli import repos as repos_mod

    alpha = tmp_root / "repos/alpha-api"
    wt_dir = tmp_root / "repos/_worktrees/alpha"
    owner = wt_dir / "owner"
    wt_dir.mkdir(parents=True)
    git(["worktree", "add", "-q", "-b", "feature/owner", str(owner)], alpha)
    outside = tmp_root / "repos/_worktrees/alpha-sibling/outside"
    outside.parent.mkdir()
    git(["worktree", "add", "-q", "-b", "feature/outside", str(outside)], alpha)
    (owner / "README.md").write_text("owner edit\n")
    (owner / "owner-untracked.txt").write_text("keep owner state\n")
    owner_before = {
        "head": git(["rev-parse", "HEAD"], owner).strip(),
        "branch": git(["branch", "--show-current"], owner).strip(),
        "readme": (owner / "README.md").read_bytes(),
        "untracked": (owner / "owner-untracked.txt").read_bytes(),
        "git_link": (owner / ".git").read_bytes(),
    }
    outside_before = (git(["rev-parse", "HEAD"], outside), (outside / ".git").read_bytes())

    dangling = wt_dir / "dangling"
    dangling.mkdir()
    (dangling / ".git").write_text("gitdir: /missing/registered-worktree\n")
    dangling_contents = (dangling / ".git").read_bytes()

    unrelated = wt_dir / "unrelated-checkout"
    git(["clone", "-q", str(tmp_root.parent / "alpha-remote.git"), str(unrelated)], tmp_root.parent)
    unrelated_contents = (unrelated / "README.md").read_bytes()
    _advance_remote(tmp_root)
    old_origin = git(["rev-parse", "origin/main"], alpha).strip()
    old_head = git(["rev-parse", "HEAD"], alpha).strip()
    remote_head = git(["rev-parse", "refs/heads/main"], tmp_root.parent / "alpha-remote.git").strip()
    repairs: list[str] = []
    real_run_git = repos_mod.run_git

    def record_repairs(commands: list[str], cwd: Path) -> str:
        if commands[:2] == ["worktree", "repair"]:
            repairs.extend(commands[2:])
        return real_run_git(commands, cwd)

    monkeypatch.setattr(repos_mod, "run_git", record_repairs)

    result = run(["--json", "repos", "sync", "alpha", *args])

    assert result.code == 0, result.err
    assert repairs == [str(owner.resolve())]
    assert owner_before == {
        "head": git(["rev-parse", "HEAD"], owner).strip(),
        "branch": git(["branch", "--show-current"], owner).strip(),
        "readme": (owner / "README.md").read_bytes(),
        "untracked": (owner / "owner-untracked.txt").read_bytes(),
        "git_link": (owner / ".git").read_bytes(),
    }
    assert (git(["rev-parse", "HEAD"], outside), (outside / ".git").read_bytes()) == outside_before
    assert (dangling / ".git").read_bytes() == dangling_contents
    assert (unrelated / "README.md").read_bytes() == unrelated_contents
    if "--pull" in args:
        assert git(["rev-parse", "origin/main"], alpha).strip() == remote_head
        assert git(["rev-parse", "HEAD"], alpha).strip() == remote_head
        assert (alpha / "NEW.md").read_text() == "landed upstream\n"
    elif "--fetch" in args:
        assert git(["rev-parse", "origin/main"], alpha).strip() == remote_head
        assert git(["rev-parse", "HEAD"], alpha).strip() == old_head
        assert old_origin != remote_head
    else:
        assert git(["rev-parse", "origin/main"], alpha).strip() == old_origin
        assert git(["rev-parse", "HEAD"], alpha).strip() == old_head


def test_sync_reports_registered_broken_worktree_before_fetch(tmp_root: Path, run, monkeypatch):
    from agent_cli import repos as repos_mod

    alpha = tmp_root / "repos/alpha-api"
    wt_dir = tmp_root / "repos/_worktrees/alpha"
    owner = wt_dir / "owner"
    broken = wt_dir / "broken"
    deleted = wt_dir / "deleted"
    wt_dir.mkdir(parents=True)
    git(["worktree", "add", "-q", "-b", "feature/owner", str(owner)], alpha)
    git(["worktree", "add", "-q", "-b", "feature/broken", str(broken)], alpha)
    git(["worktree", "add", "-q", "-b", "feature/deleted", str(deleted)], alpha)
    (owner / "README.md").write_text("owner tracked edit\n")
    (owner / "owner-untracked.txt").write_text("owner untracked\n")
    owner_before = (
        git(["rev-parse", "HEAD"], owner),
        git(["branch", "--show-current"], owner),
        (owner / "README.md").read_bytes(),
        (owner / "owner-untracked.txt").read_bytes(),
        (owner / ".git").read_bytes(),
    )
    shutil.rmtree(deleted)
    pointer = broken / ".git"
    _overwrite(pointer, b"gitdir: /missing/worktree-admin-dir\n")
    broken_pointer_before = pointer.read_bytes()
    dangling = wt_dir / "dangling"
    dangling.mkdir()
    (dangling / ".git").write_text("gitdir: /missing/unregistered\n")
    dangling_before = (dangling / ".git").read_bytes()
    registrations_before = git(["worktree", "list", "--porcelain"], alpha)
    inventory = _registrations(alpha)
    assert all(_native(path) in inventory for path in (owner, broken, deleted))
    assert "prunable" in inventory[_native(deleted)]
    assert _native(dangling) not in inventory
    _advance_remote(tmp_root)
    origin_before = git(["rev-parse", "origin/main"], alpha).strip()
    head_before = git(["rev-parse", "HEAD"], alpha).strip()
    bare_before = git(["rev-parse", "refs/heads/main"], tmp_root.parent / "alpha-remote.git").strip()

    var_alias = Path(str(wt_dir).replace("/private/var/", "/var/", 1))
    case_alias = tmp_root / "repos/_WORKTREES/alpha"
    alias_target = (
        var_alias if var_alias != wt_dir and var_alias.is_dir() and var_alias.samefile(wt_dir) else None
    )
    alias_relative = "repos/_worktrees-alias/alpha"
    alias_parent = tmp_root / alias_relative
    if alias_target is None and case_alias.is_dir() and case_alias.samefile(wt_dir):
        alias_relative = "repos/_WORKTREES/alpha"
        alias_parent = tmp_root / alias_relative
    else:
        alias_parent.parent.mkdir()
        try:
            alias_parent.symlink_to(alias_target or wt_dir, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"filesystem has no usable path alias and cannot create a symlink: {exc}")
    assert alias_parent.samefile(wt_dir)
    if alias_target is not None:
        assert Path(os.readlink(alias_parent)) == alias_target
    registry = tmp_root / "agent.yaml"
    registry.write_text(registry.read_text().replace("repos/_worktrees/alpha", alias_relative))
    assert alias_relative in registry.read_text()

    commands: list[list[str]] = []
    real_run_git = repos_mod.run_git

    def record_git(args: list[str], cwd: Path) -> str:
        commands.append(args)
        return real_run_git(args, cwd)

    monkeypatch.setattr(repos_mod, "run_git", record_git)
    result = run(["--json", "repos", "sync", "alpha", "--pull"])

    assert result.code != 0
    assert "registered worktree repair failed" in result.err
    assert str(broken.resolve()) in result.err
    assert "unable to locate repository" in result.err
    assert [command[2] for command in commands if command[:2] == ["worktree", "repair"]] == [
        str(broken.resolve())
    ]
    assert "fetch" not in [arg for command in commands for arg in command]
    assert not any(command[:1] == ["merge"] for command in commands)
    assert git(["rev-parse", "origin/main"], alpha).strip() == origin_before
    assert git(["rev-parse", "HEAD"], alpha).strip() == head_before
    assert git(["rev-parse", "refs/heads/main"], tmp_root.parent / "alpha-remote.git").strip() == bare_before
    assert git(["worktree", "list", "--porcelain"], alpha) == registrations_before
    assert (
        git(["rev-parse", "HEAD"], owner),
        git(["branch", "--show-current"], owner),
        (owner / "README.md").read_bytes(),
        (owner / "owner-untracked.txt").read_bytes(),
        (owner / ".git").read_bytes(),
    ) == owner_before
    assert (dangling / ".git").read_bytes() == dangling_before
    pointer_after = pointer.read_bytes()
    repaired_gitdir = Path(pointer_after.decode().strip().removeprefix("gitdir: "))
    assert broken_pointer_before == b"gitdir: /missing/worktree-admin-dir\n"
    assert repaired_gitdir.samefile(alpha / ".git/worktrees/broken")
    assert pointer_after != broken_pointer_before


@pytest.mark.skipif(os.name != "nt", reason="requires Windows-native Path behavior")
def test_sync_accepts_forward_slash_porcelain_paths_on_windows(tmp_root: Path, run, monkeypatch):
    from agent_cli import repos as repos_mod

    alpha = tmp_root / "repos/alpha-api"
    wt_dir = tmp_root / "repos/_worktrees/alpha"
    worktree = wt_dir / "windows path"
    wt_dir.mkdir(parents=True)
    git(["worktree", "add", "-q", "-b", "feature/windows", str(worktree)], alpha)
    real_run_git = repos_mod.run_git
    repairs: list[str] = []

    def windows_porcelain(args: list[str], cwd: Path) -> str:
        if args[:2] == ["worktree", "repair"]:
            repairs.extend(args[2:])
        output = real_run_git(args, cwd)
        if args == ["worktree", "list", "--porcelain"]:
            return output.replace("\\", "/")
        return output

    monkeypatch.setattr(repos_mod, "run_git", windows_porcelain)
    result = run(["--json", "repos", "sync", "alpha"])

    assert result.code == 0, result.err
    assert repairs == [str(worktree.resolve())]


@pytest.mark.skipif(os.name != "nt", reason="hidden is a Windows file attribute")
def test_broken_pointer_setup_overwrites_a_hidden_git_file_on_windows(tmp_root: Path):
    alpha = tmp_root / "repos/alpha-api"
    worktree = tmp_root / "repos/_worktrees/alpha/hidden"
    worktree.parent.mkdir(parents=True)
    hide_dot_git = ["-c", "core.hideDotFiles=dotGitOnly"]
    git([*hide_dot_git, "worktree", "add", "-q", "-b", "feature/hidden", str(worktree)], alpha)
    pointer = worktree / ".git"
    original = pointer.read_bytes()
    assert _hidden(pointer)
    with pytest.raises(PermissionError):
        pointer.write_text("gitdir: /missing/worktree-admin-dir\n")
    assert pointer.read_bytes() == original

    _overwrite(pointer, b"gitdir: /missing/worktree-admin-dir\n")

    assert pointer.read_bytes() == b"gitdir: /missing/worktree-admin-dir\n"
    assert _hidden(pointer)


def _advance_remote(tmp_root: Path) -> None:
    """Land a commit on the bare remote's main so the alpha clone falls behind."""
    other = tmp_root.parent / "alpha-other"
    git(["clone", "-q", str(tmp_root.parent / "alpha-remote.git"), str(other)], tmp_root.parent)
    git(["config", "user.email", "owner@example.com"], other)
    git(["config", "user.name", "Owner"], other)
    (other / "NEW.md").write_text("landed upstream\n")
    git(["add", "."], other)
    git(["commit", "-q", "-m", "T-2 upstream"], other)
    git(["push", "-q", "origin", "main"], other)


def test_sync_pull_fast_forwards_the_default_branch(tmp_root: Path, _root_template: Path, run):
    from agent_cli import repos as repos_mod

    alpha = tmp_root / "repos/alpha-api"
    bare, template_bare = tmp_root.parent / "alpha-remote.git", _root_template / "alpha-remote.git"
    assert Path(git(["remote", "get-url", "origin"], alpha).strip()).samefile(bare)
    assert Path(repos_mod.load_registry(tmp_root).require("alpha").remote).samefile(bare)
    template_head = git(["rev-parse", "refs/heads/main"], template_bare).strip()
    _advance_remote(tmp_root)
    advanced = git(["rev-parse", "refs/heads/main"], bare).strip()
    assert advanced != template_head
    r = run(["--json", "repos", "sync", "alpha", "--pull"])
    assert r.code == 0, r.err
    assert "pulled (1 commit(s))" in r.json[0]["actions"]
    assert (alpha / "NEW.md").exists()
    assert r.json[0]["behind"] == 0 and r.json[0]["remote_ok"] is True
    assert git(["rev-parse", "origin/main"], alpha).strip() == advanced
    assert git(["rev-parse", "HEAD"], alpha).strip() == advanced
    assert git(["rev-parse", "refs/heads/main"], template_bare).strip() == template_head
    # a second pull is a no-op, not an error
    r = run(["--json", "repos", "sync", "alpha", "--pull"])
    assert "already current" in r.json[0]["actions"]


def test_sync_pull_leaves_a_dirty_clone_alone(tmp_root: Path, run):
    alpha = tmp_root / "repos/alpha-api"
    _advance_remote(tmp_root)
    (alpha / "wip.txt").write_text("the owner's working state")
    r = run(["--json", "repos", "sync", "alpha", "--pull"])
    assert r.json[0]["actions"] == ["not pulled (1 uncommitted change(s))"]
    assert not (alpha / "NEW.md").exists()
    assert (alpha / "wip.txt").exists()


def test_sync_pull_leaves_a_feature_branch_alone(tmp_root: Path, run):
    alpha = tmp_root / "repos/alpha-api"
    _advance_remote(tmp_root)
    git(["checkout", "-q", "-b", "feature/wip"], alpha)
    r = run(["--json", "repos", "sync", "alpha", "--pull"])
    assert r.json[0]["actions"] == ["not pulled (on feature/wip, not main)"]
    assert not (alpha / "NEW.md").exists()


def test_registry_allows_short_equal_to_name(tmp_root: Path, run):
    reg = tmp_root / "agent.yaml"
    reg.write_text(
        "version: 1\nrepos:\n  - name: Gamma\n    short: gamma\n    path: ../x\n    remote: r\n  - name: Delta\n    path: ../y\n    remote: r\n"
    )
    assert run(["repos", "list"]).code == 0
    reg.write_text(
        "version: 1\nrepos:\n  - name: Gamma\n    path: ../x\n    remote: r\n  - name: Other\n    short: GAMMA\n    path: ../y\n    remote: r\n"
    )
    r = run(["repos", "list"])
    assert r.code == 2
    assert "duplicate" in r.err


def test_registry_validation(tmp_root: Path, run):
    (tmp_root / "agent.yaml").write_text("version: 1\nrepos:\n  - name: x\n    path: /abs\n")
    r = run(["repos", "list"])
    assert r.code == 2
    assert "missing remote" not in r.err
    assert "relative" in r.err


def test_dispatch_permission_mode_and_extra_args_are_validated(tmp_root: Path, run):
    """T5 item 8. `validate_dispatch` covered every `dispatch:` key except these two.
    Both are read on every launch (`rules.build_options`, `_repo_extra_args`). A bad value
    reached `ClaudeAgentOptions` and failed there, after the worktree and the record exist.
    That is the expensive place to find out."""
    from agent_cli import repos as repos_mod
    from agent_cli import repos_validate

    assert (
        repos_validate.validate_dispatch({"permission_mode": "acceptEdits", "extra_args": ["--foo", "bar"]})
        == []
    )
    assert repos_validate.validate_dispatch({"permission_mode": None, "extra_args": None}) == []
    for mode in repos_mod.PERMISSION_MODES:
        assert repos_validate.validate_dispatch({"permission_mode": mode}) == []

    problems = repos_validate.validate_dispatch({"permission_mode": "acceptedits"})
    assert problems == [f"dispatch.permission_mode must be one of {', '.join(repos_mod.PERMISSION_MODES)}"]

    for bad in ("--foo", ["--foo", 3], ["--foo", "  "], {}):
        problems = repos_validate.validate_dispatch({"extra_args": bad})
        assert problems and "dispatch.extra_args must be a list" in problems[0], bad

    # and the registry refuses to load with either of them wrong
    reg = tmp_root / "agent.yaml"
    reg.write_text(
        reg.read_text().replace("permission_mode: acceptEdits", "permission_mode: yolo"), encoding="utf-8"
    )
    r = run(["repos", "list"])
    assert r.code == 2 and "dispatch.permission_mode" in r.err


@pytest.mark.parametrize("bad", ["tool --version", [""], ["   "], [5], {}])
def test_dispatch_probes_require_a_list_of_nonempty_commands(bad):
    from agent_cli.repos_validate import validate_dispatch

    assert validate_dispatch({"probes": bad}) == [
        "dispatch.probes must be a list of non-empty shell command strings"
    ]


def test_dispatch_probes_accept_any_tool_and_an_empty_default():
    from agent_cli.repos_validate import validate_dispatch

    assert validate_dispatch({}) == []
    assert validate_dispatch({"probes": []}) == []
    assert validate_dispatch({"probes": ["unusual-tool --version", "other-tool -V"]}) == []


def test_a_bad_hooks_block_is_a_config_error():
    """Owner: a bad `hooks:` block voids every switch, and doctor no longer says so."""
    from agent_cli import repos_validate

    problems = repos_validate.validate_registry_data(
        {"version": 1, "repos": [], "hooks": {"no-chaining": False}}
    )
    assert any("cannot be switched" in p for p in problems)
    assert not repos_validate.validate_registry_data(
        {"version": 1, "repos": [], "hooks": {"style-guard": False}}
    )


def test_the_retired_model_from_key_fails_validation():
    """Launch refused it while validation passed it."""
    from agent_cli.repos_validate import validate_dispatch

    [problem] = validate_dispatch({"model_from": ".claude/settings.json"})
    assert "no longer read" in problem and "remove it" in problem
