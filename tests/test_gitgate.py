"""The commit and push boundaries (ADR-0026): the staged-content gate, the push refusal, the
hook installer, the export, and the Bash guard against skipping the hooks."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from agent_cli import export, gitgate
from agent_cli.core import AgentError
from agent_cli.hooks import commit_guard as guard_module

# A `#!/bin/sh` stub runner and the executable bit are POSIX; on Windows the real agent.exe is
# exercised by `test_the_real_gate_hooks_run_on_windows` instead.
POSIX_STUB = pytest.mark.skipif(os.name == "nt", reason="POSIX stub runner or executable bit")

FORMATTED = "x = 1\n\n\ndef f() -> int:\n    return x\n"
UNFORMATTED = "x=1\ndef f()->int:\n  return x\n"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "commit.gpgsign", "false")
    return path


def _stage(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _git(repo, "add", rel)


# ------------------------------------------------------------------ pre-commit


def test_clean_staged_python_passes(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "r")
    _stage(repo, "src/pkg/a.py", FORMATTED)
    assert gitgate.pre_commit(repo) == []


def test_the_staged_blob_is_checked_not_the_working_tree(tmp_path: Path) -> None:
    """The failure this gate exists for: the file on disk is fine, the staged one is not."""
    repo = _repo(tmp_path / "r")
    _stage(repo, "src/pkg/a.py", UNFORMATTED)
    (repo / "src/pkg/a.py").write_text(FORMATTED, encoding="utf-8")
    problems = gitgate.pre_commit(repo)
    assert any("not formatted" in p for p in problems)
    # and the reverse: a formatted stage with an unformatted working copy passes
    _stage(repo, "src/pkg/a.py", FORMATTED)
    (repo / "src/pkg/a.py").write_text(UNFORMATTED, encoding="utf-8")
    assert gitgate.pre_commit(repo) == []


def test_the_gate_never_writes_the_index_or_the_working_tree(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "r")
    _stage(repo, "src/pkg/a.py", UNFORMATTED)
    before = _git(repo, "ls-files", "-s")
    gitgate.pre_commit(repo)
    assert _git(repo, "ls-files", "-s") == before
    assert (repo / "src/pkg/a.py").read_text(encoding="utf-8") == UNFORMATTED


def test_lint_errors_block(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "r")
    _stage(repo, "src/pkg/a.py", "import os\n")
    assert any("ruff check" in p for p in gitgate.pre_commit(repo))


def test_python_outside_src_and_tests_is_not_checked(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "r")
    _stage(repo, "scripts/a.py", UNFORMATTED)
    assert gitgate.pre_commit(repo) == []


def test_a_deleted_file_is_not_read(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "r")
    _stage(repo, "src/pkg/a.py", FORMATTED)
    _git(repo, "commit", "-qm", "base")
    _git(repo, "rm", "-q", "src/pkg/a.py")
    assert gitgate.pre_commit(repo) == []


def test_instruction_budget_counts_every_staged_instruction_file(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "r")
    half = gitgate.INSTRUCTION_BUDGET // 2 + 10
    _stage(repo, "AGENTS.md", "a" * half)
    assert gitgate.pre_commit(repo) == []
    _stage(repo, "sub/AGENTS.md", "b" * half)
    assert any("instruction files total" in p for p in gitgate.pre_commit(repo))


# ------------------------------------------------------------------ pre-push


def test_pre_push_refuses_in_an_export_only_checkout_unless_allowed(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "r")
    assert gitgate.pre_push(repo, {}) == []  # a public clone pushes to its fork freely
    _git(repo, "config", gitgate.EXPORT_ONLY_KEY, "true")
    assert gitgate.pre_push(repo, {}) == [gitgate.PUSH_REFUSAL]
    assert gitgate.pre_push(repo, {gitgate.ALLOW_PUSH_ENV: "1"}) == []
    assert gitgate.pre_push(repo, {gitgate.ALLOW_PUSH_ENV: "yes"}) == [gitgate.PUSH_REFUSAL]


def _clone_of(tmp_path: Path, name: str, *, marker: bool, role: str | None) -> Path:
    origin = _repo(tmp_path / f"{name}-origin")
    _stage(
        origin, export.EXPORT_MARKER if marker else "a.txt", export.EXPORT_MARKER_TEXT if marker else "a\n"
    )
    _git(origin, "commit", "-qm", "history")
    clone = tmp_path / name
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    if role is not None:
        (clone / "agent.local.yaml").write_text(f"harness:\n  role: {role}\n", encoding="utf-8")
    return clone


def test_a_source_clone_without_the_marker_is_set_export_only(tmp_path: Path) -> None:
    clone = _clone_of(tmp_path, "private-clone", marker=False, role="source")
    state, detail = gitgate.settle_export_only(clone)
    assert state == "done" and "refused" in detail and "agent export" in detail
    assert gitgate.export_only_setting(clone) is True


def test_a_marker_clone_is_set_false(tmp_path: Path) -> None:
    clone = _clone_of(tmp_path, "public-clone", marker=True, role="source")
    assert gitgate.settle_export_only(clone)[0] == "done"
    assert gitgate.export_only_setting(clone) is False


def test_a_consumer_clone_without_the_marker_is_left_unset(tmp_path: Path) -> None:
    clone = _clone_of(tmp_path, "consumer-clone", marker=False, role="consumer")
    state, detail = gitgate.settle_export_only(clone)
    assert state == "skipped" and "set it yourself" in detail
    assert gitgate.export_only_setting(clone) is None


@pytest.mark.parametrize("local", [None, "harness: [\n"])
def test_a_missing_or_unparseable_role_file_fails_closed(tmp_path: Path, local: str | None) -> None:
    clone = _clone_of(tmp_path, "c", marker=False, role=None)
    if local is not None:
        (clone / "agent.local.yaml").write_text(local, encoding="utf-8")
    assert gitgate.settle_export_only(clone)[0] == "done"
    assert gitgate.export_only_setting(clone) is True


@pytest.mark.parametrize(("marker", "preset"), [(False, "false"), (True, "true")])
def test_a_preset_value_is_preserved_in_both_directions(tmp_path: Path, marker: bool, preset: str) -> None:
    clone = _clone_of(tmp_path, "c", marker=marker, role="source")
    _git(clone, "config", gitgate.EXPORT_ONLY_KEY, preset)
    assert gitgate.settle_export_only(clone)[0] == "already"
    assert gitgate.export_only_setting(clone) is (preset == "true")


@POSIX_STUB
def test_a_source_clone_push_through_the_installed_hook_is_refused(tmp_path: Path) -> None:
    import sys

    clone = _clone_of(tmp_path, "private-clone", marker=False, role="source")
    _git(clone, "config", "user.email", "test@example.com")
    _git(clone, "config", "user.name", "Test")
    runner = clone / ".venv/bin/agent"
    runner.parent.mkdir(parents=True)
    runner.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m agent_cli "$@"\n', encoding="utf-8")
    runner.chmod(0o755)
    (clone / "bin").mkdir()
    (clone / "bin/agent").write_text("", encoding="utf-8")
    gitgate.install(clone)
    gitgate.settle_export_only(clone)
    remote = tmp_path / "public.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(clone, "remote", "set-url", "origin", str(remote))
    env = {k: v for k, v in os.environ.items() if k != gitgate.ALLOW_PUSH_ENV}
    refused = subprocess.run(
        ["git", "push", "origin", "HEAD:refs/heads/private-unset"],
        cwd=clone,
        capture_output=True,
        text=True,
        env=env,
    )
    assert refused.returncode != 0
    assert "private-unset" not in _git(remote, "branch", "--list")

    _git(clone, "config", gitgate.EXPORT_ONLY_KEY, "false")
    allowed = subprocess.run(
        ["git", "push", "origin", "HEAD:refs/heads/ok"], cwd=clone, capture_output=True, text=True, env=env
    )
    assert allowed.returncode == 0, allowed.stderr


# ------------------------------------------------------------------ install


def test_install_writes_both_hooks_and_is_idempotent(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "r")
    assert gitgate.install(repo) == {"pre-commit": "installed", "pre-push": "installed"}
    assert gitgate.inspect(repo) == {"pre-commit": "installed", "pre-push": "installed"}
    assert gitgate.install(repo) == {"pre-commit": "already installed", "pre-push": "already installed"}
    hook = gitgate.hooks_dir(repo) / "pre-commit"
    assert os.name == "nt" or hook.stat().st_mode & 0o111  # Windows has no executable bit


def test_a_stale_hook_of_ours_is_updated_and_a_foreign_one_is_left(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "r")
    hooks = gitgate.hooks_dir(repo)
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "pre-commit").write_text(f"#!/bin/sh\n{gitgate.MARKER} pre-commit\nold\n", encoding="utf-8")
    (hooks / "pre-push").write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
    assert gitgate.inspect(repo) == {"pre-commit": "stale", "pre-push": "foreign"}
    result = gitgate.install(repo)
    assert result["pre-commit"] == "updated"
    assert "foreign" in result["pre-push"]
    assert (hooks / "pre-push").read_text(encoding="utf-8") == "#!/bin/sh\necho mine\n"


@pytest.mark.parametrize("marker", gitgate.LEGACY_PUSH_MARKERS)
def test_the_retired_push_hook_is_replaced_by_the_gate(tmp_path: Path, marker: str) -> None:
    """The retired standalone push hook runs a subcommand that no longer exists, so every push
    from that clone would fail. Install replaces it with the gate's own pre-push."""
    repo = _repo(tmp_path / "r")
    hooks = gitgate.hooks_dir(repo)
    hooks.mkdir(parents=True, exist_ok=True)
    legacy = f'#!/bin/sh\n{marker}{{"terms": null}}\nexec "$agent" --root "$root" retired pre-push\n'
    (hooks / "pre-push").write_text(legacy, encoding="utf-8")
    assert gitgate.inspect(repo)["pre-push"] == "legacy"
    assert "retired push hook" in gitgate.install(repo)["pre-push"]
    assert (hooks / "pre-push").read_text(encoding="utf-8") == gitgate.script("pre-push")
    assert gitgate.inspect(repo)["pre-push"] == "installed"


@pytest.mark.parametrize(
    "text",
    [
        "#!/bin/sh\necho mine\n",
        "#!/bin/sh\n# my own hook, unrelated to agent-push\nexit 0\n",
        f"#!/bin/sh\necho first\n{gitgate.LEGACY_PUSH_MARKERS[1]}\n",  # the marker, but not on line 2
    ],
)
def test_a_hook_without_the_retired_marker_is_never_touched(tmp_path: Path, text: str) -> None:
    repo = _repo(tmp_path / "r")
    hooks = gitgate.hooks_dir(repo)
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "pre-push").write_text(text, encoding="utf-8")
    assert gitgate.inspect(repo)["pre-push"] == "foreign"
    assert "foreign" in gitgate.install(repo)["pre-push"]
    assert (hooks / "pre-push").read_text(encoding="utf-8") == text


def test_the_retired_marker_on_pre_commit_is_not_ours_to_remove(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "r")
    hooks = gitgate.hooks_dir(repo)
    hooks.mkdir(parents=True, exist_ok=True)
    text = f"#!/bin/sh\n{gitgate.LEGACY_PUSH_MARKERS[0]}{{}}\n"
    (hooks / "pre-commit").write_text(text, encoding="utf-8")
    assert gitgate.inspect(repo)["pre-commit"] == "foreign"
    gitgate.install(repo)
    assert (hooks / "pre-commit").read_text(encoding="utf-8") == text


def test_hooks_are_written_with_lf_and_a_crlf_hook_of_ours_is_rewritten(tmp_path: Path) -> None:
    """Fresh Windows clone, 2026-09-29: write_text translated LF to CRLF, doctor read the gate's
    own pre-push as foreign (a false push-hook conflict), and sh may choke on `do\\r`."""
    repo = _repo(tmp_path / "r")
    gitgate.install(repo)
    hook = gitgate.hooks_dir(repo) / "pre-push"
    assert b"\r" not in hook.read_bytes()
    hook.write_bytes(hook.read_bytes().replace(b"\n", b"\r\n"))  # what the old build left
    assert gitgate.inspect(repo)["pre-push"] == "stale"
    assert gitgate.install(repo)["pre-push"] == "updated"
    assert b"\r" not in hook.read_bytes()


def test_a_linked_worktree_shares_the_main_checkouts_hooks(tmp_path: Path) -> None:
    """Manager worktrees (ADR-0023) must be gated too; git reads hooks from the common dir."""
    repo = _repo(tmp_path / "r")
    _stage(repo, "README", "x\n")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "worktree", "add", "-q", str(tmp_path / "wt"), "-b", "side")
    gitgate.install(repo)
    assert gitgate.hooks_dir(tmp_path / "wt") == gitgate.hooks_dir(repo)
    assert gitgate.inspect(tmp_path / "wt") == {"pre-commit": "installed", "pre-push": "installed"}


@POSIX_STUB
def test_a_hook_without_the_executable_bit_is_stale_and_install_fixes_it(tmp_path: Path) -> None:
    """git skips a non-executable hook without a word, so it must not read as installed."""
    repo = _repo(tmp_path / "r")
    gitgate.install(repo)
    hook = gitgate.hooks_dir(repo) / "pre-commit"
    hook.chmod(0o644)
    assert gitgate.inspect(repo)["pre-commit"] == "stale"
    assert gitgate.install(repo)["pre-commit"] == "updated"
    assert hook.stat().st_mode & 0o111


def test_hooks_dir_follows_core_hooks_path(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "r")
    _git(repo, "config", "core.hooksPath", str(tmp_path / "elsewhere"))
    assert gitgate.hooks_dir(repo) == (tmp_path / "elsewhere").resolve()
    assert gitgate.inspect(repo) == {"pre-commit": "missing", "pre-push": "missing"}


def _stub_agent(checkout: Path, log: Path, relative: str = ".venv/bin/agent") -> None:
    agent = checkout / relative
    agent.parent.mkdir(parents=True)
    agent.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> {log}\n', encoding="utf-8")
    agent.chmod(0o755)


@POSIX_STUB
def test_a_worktree_without_a_venv_runs_the_main_checkouts_agent_on_itself(tmp_path: Path) -> None:
    """The hooks dir is shared, `.venv` is not: a fresh manager worktree still gets gated."""
    repo = _repo(tmp_path / "r")
    (repo / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-qm", "base")
    log = tmp_path / "calls.log"
    _stub_agent(repo, log)
    gitgate.install(repo)
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "side")
    _stage(wt, "README", "x\n")
    _git(wt, "commit", "-qm", "from the worktree")
    assert log.read_text(encoding="utf-8").split("\n")[-2] == f"--root {wt.resolve()} git pre-commit"


@POSIX_STUB
def test_the_hook_finds_a_windows_venv_runner(tmp_path: Path) -> None:
    """A Windows venv has `.venv/Scripts/`, not `.venv/bin/agent`; the hook knowing only the
    second blocked every commit on the owner's first Windows clone (handoff item 5). It runs the
    venv's python as a module, never the agent.exe shim."""
    repo = _repo(tmp_path / "r")
    log = tmp_path / "calls.log"
    _stub_agent(repo, log, ".venv/Scripts/python.exe")
    gitgate.install(repo)
    _stage(repo, "README", "x\n")
    _git(repo, "commit", "-qm", "x")
    assert (
        log.read_text(encoding="utf-8").split("\n")[-2]
        == f"-I -m agent_cli --root {repo.resolve()} git pre-commit"
    )


@pytest.mark.skipif(os.name != "nt", reason="native Windows: Git for Windows' sh runs the real venv python")
@pytest.mark.parametrize("name", gitgate.HOOKS)
def test_the_real_gate_hooks_run_on_windows(name: str, tmp_path: Path) -> None:
    """The stub tests above cannot run on Windows (a `#!` script named python.exe is not a PE
    file), so this runs each generated hook with Git's own sh against this checkout, whose
    `.venv/Scripts/python.exe` is real. Nothing is staged, so pre-commit passes. Whether
    pre-push passes depends on the checkout: setup declares a fresh clone a consumer, and the
    record gate refuses every consumer push. So the hook must reach the verdict the gates give
    when called directly."""
    import shutil

    from agent_cli import push_records

    checkout = Path(__file__).resolve().parents[1]
    if not (checkout / ".venv/Scripts/python.exe").is_file():
        pytest.skip("this checkout has no Windows venv")
    sh = shutil.which("sh") or r"C:\Program Files\Git\bin\sh.exe"
    hook = tmp_path / name
    hook.write_text(gitgate.script(name), encoding="utf-8", newline="\n")
    env = {**os.environ, gitgate.ALLOW_PUSH_ENV: "1"}
    expected: list[str] = []
    if name == "pre-push":
        expected = gitgate.pre_push(checkout, env) or push_records.problems(checkout, [])
    proc = subprocess.run(
        [sh, str(hook)], cwd=checkout, input="", capture_output=True, text=True, env=env, timeout=120
    )
    assert proc.returncode == (1 if expected else 0), proc.stdout + proc.stderr
    assert "gate unavailable" not in proc.stderr
    assert all(line in proc.stderr for line in expected), proc.stderr


def test_the_hook_fails_closed_without_the_checkouts_venv(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "r")
    gitgate.install(repo)
    _stage(repo, "README", "x\n")
    proc = subprocess.run(["git", "commit", "-qm", "x"], cwd=repo, capture_output=True, text=True)
    assert proc.returncode != 0
    assert "gate unavailable" in proc.stderr


# ------------------------------------------------------------------ export


PUBLIC = {"name": "Pat Public", "email": "pat@public.example"}
SOURCE_ORIGIN = "https://github.com/me/public.git"
NEW_REMOTE = "git@github.com:me/new.git"


def _identity(repo: Path, export_block: object) -> None:
    """The untracked agent.yaml the export reads `export.identity` from."""
    data: dict[str, object] = {"repos": []}
    if export_block is not None:
        data["export"] = export_block
    (repo / "agent.yaml").write_text(json.dumps(data), encoding="utf-8")


def _source(tmp_path: Path, export_block: object = {"identity": PUBLIC}) -> Path:  # noqa: B006 -- never mutated
    repo = _repo(tmp_path / "src-repo")
    _git(repo, "remote", "add", "origin", SOURCE_ORIGIN)
    _stage(repo, "README.md", "hello\n")
    _git(repo, "commit", "-qm", "private history one")
    _stage(repo, "notes.txt", "second\n")
    _git(repo, "commit", "-qm", "private history two")
    _identity(repo, export_block)
    return repo


# Refused exports read the source and build nothing, so those tables share one per module.
@pytest.fixture(scope="module")
def shared_source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _source(tmp_path_factory.mktemp("export"))


@pytest.fixture(scope="module")
def configured_source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    repo = _source(tmp_path_factory.mktemp("export-config"))
    _git(repo, "config", "user.name", "Git Config")
    _git(repo, "config", "user.email", "git@config.example")
    return repo


def test_export_squashes_head_into_one_commit_and_stops_before_push(tmp_path: Path) -> None:
    repo = _source(tmp_path)
    data = export.build(repo, tmp_path / "out", message="public", remote=NEW_REMOTE)
    assert data["ok"], data
    out = tmp_path / "out"
    assert _git(out, "rev-list", "--count", "HEAD").strip() == "1"
    assert _git(out, "log", "-1", "--format=%s").strip() == "public"
    assert sorted(_git(out, "ls-files").split()) == [export.EXPORT_MARKER, "README.md", "notes.txt"]
    assert not (repo / export.EXPORT_MARKER).exists(), "the marker never lands in the source tree"
    assert _git(out, "remote", "get-url", "origin").strip() == NEW_REMOTE
    assert data["origin"] == NEW_REMOTE and data["source_origin"] == SOURCE_ORIGIN
    text = export.format_result(data)
    assert NEW_REMOTE in text and SOURCE_ORIGIN in text
    assert data["push"] == f"git -C {out} push --force origin HEAD:main"
    assert "private history" not in _git(out, "log", "--all", "--format=%B")


def test_export_restores_submodule_pointers(tmp_path: Path) -> None:
    repo = _source(tmp_path)
    sha = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{sha},vendor/thing")
    _git(repo, "commit", "-qm", "add gitlink")
    data = export.build(repo, tmp_path / "out", message="public", remote=NEW_REMOTE)
    assert data["gitlinks"] == ["vendor/thing"]
    entry = _git(tmp_path / "out", "ls-tree", "HEAD", "vendor/thing")
    assert entry.startswith(f"160000 commit {sha}")


def test_export_stamps_the_configured_identity_not_the_checkouts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The checkout's own email used to reach the export commit, and the commit had to be
    re-identified by hand on every publish. `export.identity` authors and commits it."""
    repo = _source(tmp_path)
    _git(repo, "config", "user.email", "me@zorp.example")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "shell@zorp.example")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "shell@zorp.example")
    data = export.build(repo, tmp_path / "out", message="public", remote=NEW_REMOTE)
    assert data["ok"], data
    assert data["author"] == "Pat Public <pat@public.example>"
    stamped = _git(tmp_path / "out", "log", "-1", "--format=%an <%ae>|%cn <%ce>").strip()
    assert stamped == "Pat Public <pat@public.example>|Pat Public <pat@public.example>"


@pytest.mark.parametrize(
    "export_block",
    [
        None,
        {},
        {"identity": {"name": "Pat Public"}},
        {"identity": {"name": "Pat <x>", "email": "pat@public.example"}},
        {"identity": {"name": "Pat Public", "email": "not-an-address"}},
        {"identity": {"name": "Pat Public", "email": 7}},
    ],
)
def test_export_refuses_without_a_configured_identity(
    tmp_path: Path, configured_source: Path, export_block: object
) -> None:
    """Never a fallback to git config, even when it is set."""
    repo = configured_source
    _identity(repo, export_block)
    with pytest.raises(AgentError, match=r"export\.identity") as refused:
        export.build(repo, tmp_path / "out", message="m", remote=NEW_REMOTE)
    assert "agent.yaml" in str(refused.value)
    assert not (tmp_path / "out").exists(), "nothing is built before the identity is known"


def test_export_flags_a_private_set_path(tmp_path: Path) -> None:
    repo = _source(tmp_path)
    _stage(repo, "journal/2026/day.md", "x\n")
    _git(repo, "commit", "-qm", "journal")
    data = export.build(repo, tmp_path / "out", message="public", remote=NEW_REMOTE)
    assert data["private_paths"] == ["journal"]
    assert not data["ok"]
    assert data["push"] is None
    assert "NOT clean" in export.format_result(data)


def test_export_refuses_a_non_empty_or_inside_directory(tmp_path: Path) -> None:
    repo = _source(tmp_path)
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "x").write_text("x", encoding="utf-8")
    with pytest.raises(AgentError, match="not empty"):
        export.build(repo, tmp_path / "full", message="m", remote=NEW_REMOTE)
    with pytest.raises(AgentError, match="outside"):
        export.build(repo, repo / "out", message="m", remote=NEW_REMOTE)


@pytest.mark.parametrize(
    "remote",
    [
        SOURCE_ORIGIN,
        "git@github.com:Me/Public.git",
        "ssh://git@github.com/me/public",
        "https://github.com/ME/PUBLIC/",
        "git@github.com:me/public",
        "git@github.com:me/public.GIT",
        "git@github.com:me/public.GIT/",
        "https://github.com/me/public.GIT",
        "https://github.com/me/public.GIT/",
    ],
)
def test_export_refuses_the_source_repository_in_any_spelling(
    tmp_path: Path, shared_source: Path, remote: str
) -> None:
    with pytest.raises(AgentError, match="same repository"):
        export.build(shared_source, tmp_path / "out", message="m", remote=remote)
    assert not (tmp_path / "out").exists(), "nothing is built for a refused remote"


def test_export_refuses_an_https_remote_for_an_ssh_source(tmp_path: Path) -> None:
    repo = _source(tmp_path)
    _git(repo, "remote", "set-url", "origin", "git@github.com:me/public.git")
    with pytest.raises(AgentError, match="same repository"):
        export.build(repo, tmp_path / "out", message="m", remote=SOURCE_ORIGIN)
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("remote", ["git@example.com:me/new.git", "https://gitlab.com/me/new", "nonsense"])
def test_export_refuses_a_remote_that_is_not_a_github_repository(
    tmp_path: Path, shared_source: Path, remote: str
) -> None:
    with pytest.raises(AgentError, match=re.escape(remote)):
        export.build(shared_source, tmp_path / "out", message="m", remote=remote)
    assert not (tmp_path / "out").exists()


# ------------------------------------------------------------------ the Bash guard


@pytest.fixture(scope="module")
def shared_vault(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _repo(tmp_path_factory.mktemp("guard") / "vault")


def _decision(result: str) -> str:
    return json.loads(result)["hookSpecificOutput"]["permissionDecision"] if result else "allow"


@pytest.mark.parametrize(
    ("command", "decision"),
    [
        ("git commit -m fix", "allow"),
        ("git commit --no-verify -m fix", "deny"),
        ("git commit -n -m fix", "deny"),
        ("git commit -anm fix", "deny"),
        ("git commit -mn", "allow"),  # the message is "n"
        ("git commit -m 'no -n here'", "allow"),
        ("git commit -m -n", "allow"),  # the message is "-n"
        ("git commit -F- <<'EOF'\nsubject\n\ncommit -n and --no-verify are denied\nEOF", "allow"),
        ("git commit -F - -n", "deny"),
        ("git -c core.hooksPath=/dev/null commit -m fix", "deny"),
        ("git -c core.hooksPath=/tmp/x rev-parse --git-path hooks", "allow"),
        ("git push --no-verify origin main", "deny"),
        ("git push -n origin main", "allow"),  # --dry-run
        ("git status", "allow"),
    ],
)
def test_skipping_the_hooks_is_denied_in_this_checkout(
    shared_vault: Path, command: str, decision: str
) -> None:
    root = shared_vault
    payload = {"tool_name": "Bash", "cwd": str(root), "tool_input": {"command": command}}
    assert _decision(guard_module.hook_bypass_guard(root, payload)) == decision


def test_another_repos_hooks_are_its_own_business(tmp_path: Path) -> None:
    root = _repo(tmp_path / "vault")
    other = _repo(tmp_path / "product")
    payload = {"tool_name": "Bash", "cwd": str(other), "tool_input": {"command": "git commit -n -m x"}}
    assert guard_module.hook_bypass_guard(root, payload) == ""
