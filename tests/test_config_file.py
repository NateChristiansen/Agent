"""The config file (ADR-0030): `agent.yaml`, this machine's `agent.local.yaml`
overrides, and both kept private."""

import subprocess
from pathlib import Path

import pytest

from agent_cli import config_file, notes
from agent_cli.core import AgentError

VALID = "version: 1\nrepos: []\n"


def _dangling(link: Path, target: Path) -> None:
    """A real dangling symlink, or a skip on a Windows account without the symlink privilege
    (plain Windows makes junctions and hard links instead, and neither can dangle this way)."""
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks need a privilege this account lacks: {exc}")


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ------------------------------------------------------------------ which file is read


def test_agent_yaml_is_read(tmp_path: Path):
    from agent_cli import repos

    _write(tmp_path / "agent.yaml", "version: 1\nrepos: []\nowner: {name: New}\n")
    assert config_file.registry_path(tmp_path) == tmp_path / "agent.yaml"
    assert repos.owner_name(tmp_path) == "New"


def test_an_invalid_file_fails(tmp_path: Path):
    from agent_cli import repos

    _write(tmp_path / "agent.yaml", "version: 1\nrepos: nope\n")
    with pytest.raises(AgentError, match="agent.yaml"):
        repos.load_registry(tmp_path)


def test_a_dangling_link_fails(tmp_path: Path):
    from agent_cli import repos

    _dangling(tmp_path / "agent.yaml", tmp_path / "gone.yaml")
    with pytest.raises(AgentError, match="agent.yaml is missing"):
        repos.load_registry(tmp_path)


def test_a_missing_file_errors_with_its_name(tmp_path: Path):
    from agent_cli import repos

    with pytest.raises(AgentError, match="agent.yaml is missing"):
        repos.load_registry(tmp_path)


# ------------------------------------------------------------------ agent.local.yaml

SHARED = """version: 1
repos:
  - name: auth-service
    short: auth
    path: ../repos/auth-service
    remote: git@github.com:org/auth-service.git
    worktrees_dir: ../repos/_worktrees/auth
  - name: web
    path: ../repos/web
    remote: git@github.com:org/web.git
"""


def _registry(tmp_path: Path, local: str | None):
    from agent_cli import repos

    _write(tmp_path / "agent.yaml", SHARED)
    if local is not None:
        _write(tmp_path / "agent.local.yaml", local)
    return {r.short: r for r in repos.load_registry(tmp_path).repos}


def test_no_local_file_keeps_the_shared_paths(tmp_path: Path):
    reg = _registry(tmp_path, None)
    assert reg["auth"].abs_path(tmp_path) == (tmp_path / "../repos/auth-service").resolve()
    assert reg["auth"].local == ()


def test_a_local_absolute_path_overrides_only_that_key(tmp_path: Path):
    elsewhere = tmp_path / "code" / "auth"
    reg = _registry(tmp_path, f"repos:\n  auth:\n    path: {elsewhere}\n")
    assert reg["auth"].abs_path(tmp_path) == elsewhere
    assert reg["auth"].local == ("path",)
    assert reg["auth"].abs_worktrees(tmp_path) == (tmp_path / "../repos/_worktrees/auth").resolve(), (
        "inherited"
    )
    assert reg["web"].local == (), "other repos untouched"


def test_worktrees_dir_overrides_independently(tmp_path: Path):
    wt = tmp_path / "wt"
    reg = _registry(tmp_path, f"repos:\n  auth:\n    worktrees_dir: {wt}\n")
    assert reg["auth"].abs_worktrees(tmp_path) == wt
    assert reg["auth"].abs_path(tmp_path) == (tmp_path / "../repos/auth-service").resolve(), "inherited"


def test_a_tilde_expands_and_a_relative_path_resolves_from_the_clone(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    reg = _registry(tmp_path, "repos:\n  auth:\n    path: ~/code/auth\n  web:\n    path: ../elsewhere/web\n")
    assert reg["auth"].abs_path(tmp_path) == tmp_path / "home" / "code" / "auth"
    assert reg["web"].abs_path(tmp_path) == (tmp_path / "../elsewhere/web").resolve()


def test_a_repo_is_matched_by_name_too(tmp_path: Path):
    reg = _registry(tmp_path, f"repos:\n  auth-service:\n    path: {tmp_path / 'x'}\n")
    assert reg["auth"].local == ("path",)


def test_one_repo_under_both_its_name_and_short_is_refused(tmp_path: Path):
    """Rather than keep one block and silently drop the other."""
    local = (
        f"repos:\n  auth:\n    path: {tmp_path / 'a'}\n  auth-service:\n    worktrees_dir: {tmp_path / 'b'}\n"
    )
    with pytest.raises(AgentError, match="both its name and its short"):
        _registry(tmp_path, local)


@pytest.mark.parametrize(
    ("local", "problem"),
    [
        ("repos:\n  nope:\n    path: /x\n", "not a repo in agent.yaml"),
        ("repos:\n  auth:\n    remote: git@x:y.git\n", "cannot be set here"),
        ("repos:\n  auth:\n    path: null\n", "non-empty path"),
        ("repos:\n  auth:\n    path: ''\n", "non-empty path"),
        ("repos:\n  auth: {}\n", "path and/or worktrees_dir"),
        ("owner: {name: me}\n", "only `repos:`"),
        ("repos: [auth]\n", "must map a repo short"),
        ("repos: {auth: {path: [\n", "invalid YAML"),
        # falsey shapes the first draft let through as "no overrides"
        ("[]\n", "only `repos:`"),
        ("false\n", "only `repos:`"),
        ("0\n", "only `repos:`"),
        ("repos: []\n", "must map a repo short"),
        ("repos: false\n", "must map a repo short"),
        ("repos: null\n", "must map a repo short"),
        ("repos:\n", "must map a repo short"),
    ],
)
def test_anything_unexpected_in_the_local_file_fails(tmp_path: Path, local: str, problem: str):
    with pytest.raises(AgentError, match=problem):
        _registry(tmp_path, local)


@pytest.mark.parametrize("text", ["", "\n\n", "# nothing here yet\n"])
def test_an_empty_or_comment_only_local_file_overrides_nothing(tmp_path: Path, text: str):
    assert _registry(tmp_path, text)["auth"].local == ()


@pytest.mark.parametrize("text", ["null\n", "~\n", "# a note\nnull\n"])
def test_an_explicit_null_document_fails(tmp_path: Path, text: str):
    with pytest.raises(AgentError, match="only `repos:`"):
        _registry(tmp_path, text)


def test_a_dangling_local_link_fails(tmp_path: Path):
    from agent_cli import repos

    _write(tmp_path / "agent.yaml", SHARED)
    _dangling(tmp_path / "agent.local.yaml", tmp_path / "gone.yaml")
    with pytest.raises(AgentError, match="broken link"):
        repos.load_registry(tmp_path)


@pytest.mark.parametrize(
    "value",
    ["/abs/x", "~/x", r"C:\x", "C:/x", r"\\server\share\x", "\\x"],
)
@pytest.mark.parametrize("key", ["path", "worktrees_dir"])
def test_the_shared_file_refuses_absolute_and_tilde_paths_on_every_os(key: str, value: str):
    from agent_cli import repos

    entry = {"name": "x", "path": "../repos/x", "remote": "r", key: value}
    problems = repos.validate_registry_data({"version": 1, "repos": [entry]})
    assert any(f"{key} must be relative" in p for p in problems), problems


def test_a_worktree_load_checks_the_machine_hook_switches_the_hooks_read(tmp_path: Path):
    """Codex review: hooks read the main checkout's `agent.local.yaml`; a worktree
    validated only its own (or none), so a bad block there still voided every switch silently."""
    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(["git", "init", "-q", str(main)], check=True)
    subprocess.run(
        ["git", "-C", str(main), "commit", "-q", "--allow-empty", "-m", "x"],
        check=True,
        env={
            **__import__("os").environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )
    worktree = tmp_path / "wt"
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", str(worktree)], check=True)
    _write(main / "agent.local.yaml", "hooks:\n  thrash-guard: false\n  not-a-hook: false\n")
    with pytest.raises(AgentError, match="cannot be switched"):
        config_file.load_local(worktree, set())  # the worktree has no local file of its own
    _write(main / "agent.local.yaml", "hooks:\n  thrash-guard: false\n")
    assert config_file.load_local(worktree, set()) == {}
    # Codex round 2: an unreadable main-checkout file must fail too, not read as no switches.
    _write(main / "agent.local.yaml", "hooks: {thrash-guard: false\n")  # unterminated flow map
    with pytest.raises(AgentError, match="invalid YAML"):
        config_file.load_local(worktree, set())
    (main / "agent.local.yaml").unlink()
    _dangling(main / "agent.local.yaml", tmp_path / "gone.yaml")
    with pytest.raises(AgentError, match="broken link"):
        config_file.load_local(worktree, set())


# ------------------------------------------------------------------ privacy


def test_the_config_is_private_and_gitignored():
    root = Path(__file__).resolve().parents[1]
    assert "agent.yaml" in {e.path for e in notes.PRIVATE_SET}
    assert "agent.local.yaml" not in {e.path for e in notes.PRIVATE_SET}, "per machine, never linked"
    for name in ("agent.yaml", "repos.yaml", "agent.local.yaml"):  # repos.yaml: old name, still private
        proc = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", name], check=False)
        assert proc.returncode == 0, f"{name} must be gitignored"
    assert (root / "agent.example.yaml").is_file()


@pytest.mark.parametrize("name", ["repos.yaml", "agent.local.yaml", "agent.yaml"])
def test_export_refuses_a_tracked_config_under_any_name(tmp_path: Path, name: str):
    """Gitignore alone does not stop a file tracked by mistake; the export check must."""
    from agent_cli import export
    from test_gitgate import NEW_REMOTE, PUBLIC, _git, _identity, _source

    repo = _source(tmp_path)
    _write(repo / name, "version: 1\nrepos: []\n")
    _git(repo, "add", "-f", name)
    _git(repo, "commit", "-qm", "tracked by mistake")
    _identity(repo, {"identity": PUBLIC})  # the working copy export reads its identity from
    data = export.build(repo, tmp_path / "out", message="public", remote=NEW_REMOTE)
    assert not data["ok"] and name in data["private_paths"]
