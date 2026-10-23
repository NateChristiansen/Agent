"""The rest of the notes-repo split (ADR-0012): the `notes link` setup step and its memory,
the `bin/agent` root marker, and doctor's private-set rows.

`agent_cli.notes` itself is `tests/test_notes.py`. Everything here goes through `ops` or the
CLI, on a `tmp_root`.
"""

from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

import pytest

from agent_cli import gitgate, notes, ops
from agent_cli import repos as repos_mod
from agent_cli.core import AgentError, find_root
from symlinks import assert_linked
from test_setup import no_shelling, quiet_doctor, tools  # noqa: F401 -- reuse the setup fixtures


@pytest.fixture
def notes_repo(tmp_root: Path) -> Path:
    """A notes repo holding the fixture's `agent.yaml`, as the real swap leaves it: the
    registry moves out of the harness clone before setup links it back in."""
    repo = tmp_root.parent / "harness-notes"
    repo.mkdir()
    shutil.move(str(tmp_root / "agent.yaml"), str(repo / "agent.yaml"))
    return repo


def _step(op, name: str = "notes link") -> dict:
    return next(s for s in op.data["configure"] if s["step"] == name)


# ------------------------------------------------------------------ the root marker


def test_find_root_walks_up_to_bin_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "Agent"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "agent").write_text("", encoding="utf-8")
    deep = root / "features" / "001-x"
    deep.mkdir(parents=True)
    monkeypatch.delenv("AGENT_ROOT", raising=False)
    monkeypatch.chdir(deep)
    assert find_root() == root.resolve()


def test_find_root_refuses_a_directory_without_bin_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("AGENT_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(AgentError) as exc:
        find_root()
    assert "bin/agent" in exc.value.message
    with pytest.raises(AgentError) as explicit:
        find_root(str(tmp_path))
    assert "bin/agent" in explicit.value.message


def test_find_root_no_longer_needs_agent_yaml(tmp_root: Path, monkeypatch: pytest.MonkeyPatch):
    """The registry is the owner's and may be a dangling symlink in a fresh clone. The root
    is still the root."""
    (tmp_root / "agent.yaml").unlink()
    monkeypatch.delenv("AGENT_ROOT", raising=False)
    monkeypatch.chdir(tmp_root)
    assert find_root() == tmp_root.resolve()


def test_a_missing_registry_names_the_example_and_the_notes_flag(tmp_root: Path):
    (tmp_root / "agent.yaml").unlink()
    with pytest.raises(AgentError) as exc:
        repos_mod.load_registry(tmp_root)
    assert "agent.example.yaml" in exc.value.message
    assert "agent setup" in exc.value.message


# ------------------------------------------------------------------ setup --notes


def test_setup_with_notes_links_the_set_and_remembers_the_repo(
    tmp_root: Path, notes_repo: Path, tools, no_shelling, quiet_doctor, plugin_builds
):
    op = ops.setup(tmp_root, notes=str(notes_repo))
    assert op.code == 0, op.text
    step = _step(op)
    assert step["state"] == "done"
    assert str(notes_repo) in step["detail"]
    assert_linked(tmp_root / "features", notes_repo / "features")
    assert_linked(tmp_root / "agent.yaml", notes_repo / "agent.yaml")
    assert (tmp_root / "agent.yaml").read_text(encoding="utf-8").startswith("version: 1")
    assert (tmp_root / notes.NOTES_ROOT_FILE).read_text(encoding="utf-8").strip() == str(notes_repo)
    config = tomllib.loads((tmp_root / notes.CODEX_CONFIG_FILE).read_text(encoding="utf-8"))
    assert config["default_permissions"] == "agent-manager"
    assert config["approvals_reviewer"] == "auto_review"
    assert config["permissions"]["agent-manager"]["filesystem"][str(notes_repo.resolve())] == "write"
    assert config["permissions"]["agent-manager"]["network"]["domains"]["api.github.com"] == "allow"
    assert config["permissions"]["agent-manager"]["network"]["unix_sockets"]["/tmp/cc-socks"] == "allow"
    assert config["features"]["network_proxy"] is True
    assert _step(op, "codex manager profile")["state"] == "done"
    assert "alpha installed" in _step(op, "git hooks")["detail"]
    assert gitgate.inspect_review(tmp_root / "repos" / "alpha-api", tmp_root) == "installed"
    assert op.data["ok"] is True


def test_a_later_setup_reuses_the_remembered_notes_repo(
    tmp_root: Path, notes_repo: Path, tools, no_shelling, quiet_doctor, plugin_builds
):
    ops.setup(tmp_root, notes=str(notes_repo))
    again = ops.setup(tmp_root)  # no --notes this time
    assert _step(again)["state"] == "already"
    assert str(notes_repo) in _step(again)["detail"]
    assert_linked(tmp_root / "features", notes_repo / "features")  # not replaced by a plain dir
    assert _step(again, "codex manager profile")["state"] == "already"


def test_notes_flag_wins_over_the_remembered_path(
    tmp_root: Path, notes_repo: Path, tools, no_shelling, quiet_doctor, plugin_builds
):
    ops.setup(tmp_root, notes=str(notes_repo))
    other = tmp_root.parent / "other-notes"
    other.mkdir()
    op = ops.setup(tmp_root, notes=str(other))
    assert (tmp_root / notes.NOTES_ROOT_FILE).read_text(encoding="utf-8").strip() == str(other)
    assert _step(op)["state"] == "failed", "the old links point at the old repo, so they are refused"
    assert "features" in _step(op)["detail"]


def test_a_real_file_at_a_private_path_fails_the_step_but_not_the_run(
    tmp_root: Path, notes_repo: Path, tools, no_shelling, quiet_doctor, plugin_builds
):
    (tmp_root / "journal").mkdir()
    (tmp_root / "journal" / "mine.md").write_text("mine\n", encoding="utf-8")
    op = ops.setup(tmp_root, notes=str(notes_repo))
    step = _step(op)
    assert step["state"] == "failed"
    assert "journal" in step["detail"]
    assert op.data["ok"] is False
    assert (tmp_root / "journal" / "mine.md").read_text(encoding="utf-8") == "mine\n"
    assert [s["step"] for s in op.data["configure"]][-1] == "status render", "the rest still ran"


def test_setup_without_notes_creates_and_links_agent_notes(
    tmp_root: Path, tools, no_shelling, quiet_doctor, plugin_builds
):
    """A notes repo is required. A fresh clone has no private files, and setup
    with no `--notes` creates `../Agent-notes` beside it and links that."""
    (tmp_root / "agent.yaml").unlink()  # a fresh clone does not carry the gitignored registry
    shutil.copy(Path(__file__).resolve().parents[1] / "agent.example.yaml", tmp_root / "agent.example.yaml")
    op = ops.setup(tmp_root)
    step = _step(op)
    default = tmp_root.parent / "Agent-notes"
    assert step["state"] == "done", step
    assert f"created {default}" in step["detail"]
    assert (default / ".git").is_dir(), "a git repo, from notes init"
    assert_linked(tmp_root / "features", default / "features")
    assert_linked(tmp_root / "agent.yaml", default / "agent.yaml")
    # #61: a new vault's agent.yaml has no example repo, so doctor has nothing to warn about.
    assert "example-service" not in (default / "agent.yaml").read_text(encoding="utf-8")
    assert "repos: []" in (default / "agent.yaml").read_text(encoding="utf-8")
    assert (tmp_root / notes.NOTES_ROOT_FILE).read_text(encoding="utf-8").strip() == str(default)
    config = tomllib.loads((tmp_root / notes.CODEX_CONFIG_FILE).read_text(encoding="utf-8"))
    assert config["permissions"]["agent-manager"]["filesystem"][str(default.resolve())] == "write"


def test_setup_leaves_a_clean_checkout(tmp_root: Path, tools, no_shelling, quiet_doctor, plugin_builds):
    """Everything setup and normal use writes is ignored or linked into the notes
    repo, so the owner's updates always pull cleanly."""
    import subprocess

    (tmp_root / "agent.yaml").unlink()
    shutil.copy(Path(__file__).resolve().parents[1] / "agent.example.yaml", tmp_root / "agent.example.yaml")
    shutil.copy(Path(__file__).resolve().parents[1] / ".gitignore", tmp_root / ".gitignore")

    def git(*args: str) -> str:
        proc = subprocess.run(["git", *args], cwd=tmp_root, capture_output=True, text=True, check=True)
        return proc.stdout

    git("init", "-q")
    ops.setup(tmp_root)
    # What a clone tracks: everything setup left that is not ignored or linked, which in the
    # real repo is the tracked tree setup regenerates identically.
    git("add", "-A")
    # --no-verify: setup installed the harness's own pre-commit gate, which is not under test
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "clone", "--no-verify")
    assert git("ls-files", "features", "journal", "agent.yaml", ".mcp.json", "bases") == "", (
        "nothing private tracked"
    )

    # Normal use afterwards: setup again, a render, a journal write through the links, Obsidian
    # rewriting its own config (a theme choice), and a pasted image landing in attachments.
    ops.setup(tmp_root)
    ops.status_render(tmp_root)
    (tmp_root / "journal" / "2026-09-25.md").write_text("a day\n", encoding="utf-8")
    appearance = tmp_root / ".obsidian" / "appearance.json"
    appearance.parent.mkdir(parents=True, exist_ok=True)
    appearance.write_text('{"theme": "obsidian"}', encoding="utf-8")
    (tmp_root / "notes" / "attachments").mkdir(parents=True, exist_ok=True)
    (tmp_root / "notes" / "attachments" / "Pasted image.png").write_bytes(b"png")
    assert git("status", "--porcelain") == ""


def test_a_chosen_notes_path_that_does_not_exist_is_created(
    tmp_root: Path, tools, no_shelling, quiet_doctor, plugin_builds
):
    """Setup lets people put the notes repo somewhere other than
    ../Agent-notes. A new folder there is created as a notes repo, as the default is."""
    (tmp_root / "agent.yaml").unlink()
    shutil.copy(Path(__file__).resolve().parents[1] / "agent.example.yaml", tmp_root / "agent.example.yaml")
    chosen = tmp_root.parent / "my notes"
    op = ops.setup(tmp_root, notes=str(chosen))
    step = _step(op)
    assert step["state"] == "done", step
    assert f"created {chosen}" in step["detail"] and (chosen / ".git").is_dir()
    assert (tmp_root / notes.NOTES_ROOT_FILE).read_text(encoding="utf-8").strip() == str(chosen)


@pytest.mark.parametrize(
    "contents",
    [
        {"taxes.pdf": b"pdf"},
        {"journal/day.md": b"my own diary"},  # a notes-looking folder that is not a notes repo
        {".git/HEAD": b"ref: refs/heads/main\n", "src/app.py": b"print()"},  # some other git repo
    ],
)
def test_a_folder_that_is_not_a_notes_repo_is_refused(
    tmp_root: Path, tools, no_shelling, quiet_doctor, plugin_builds, contents: dict
):
    stray = tmp_root.parent / "Documents"
    for name, data in contents.items():
        (stray / name).parent.mkdir(parents=True, exist_ok=True)
        (stray / name).write_bytes(data)
    before = sorted(str(p.relative_to(stray)) for p in stray.rglob("*"))
    op = ops.setup(tmp_root, notes=str(stray))
    step = _step(op)
    assert step["state"] == "failed" and "is not a notes repo" in step["detail"]
    assert sorted(str(p.relative_to(stray)) for p in stray.rglob("*")) == before, "nothing written"
    assert not (tmp_root / notes.NOTES_ROOT_FILE).exists(), "nothing to remember"
    assert [s["step"] for s in op.data["configure"]][-1] == "status render"


def test_an_empty_git_repo_becomes_the_notes_repo(
    tmp_root: Path, tools, no_shelling, quiet_doctor, plugin_builds
):
    import subprocess

    (tmp_root / "agent.yaml").unlink()
    shutil.copy(Path(__file__).resolve().parents[1] / "agent.example.yaml", tmp_root / "agent.example.yaml")
    fresh = tmp_root.parent / "fresh-notes"
    fresh.mkdir()
    subprocess.run(["git", "init", "-q", str(fresh)], check=True)
    step = _step(ops.setup(tmp_root, notes=str(fresh)))
    assert step["state"] == "done" and f"created {fresh}" in step["detail"]
    assert notes.is_skeleton(fresh)


def test_a_notes_folder_need_not_be_a_git_repo(
    tmp_root: Path, notes_repo: Path, tools, no_shelling, quiet_doctor, plugin_builds
):
    """A notes folder has no reason to be required a git repo. The
    notes_repo fixture holds agent.yaml and no .git; setup links it and leaves git alone."""
    step = _step(ops.setup(tmp_root, notes=str(notes_repo)))
    assert step["state"] == "done", step
    assert not (notes_repo / ".git").exists()


def test_a_remembered_notes_repo_that_moved_is_not_recreated(
    tmp_root: Path, notes_repo: Path, tools, no_shelling, quiet_doctor, plugin_builds
):
    ops.setup(tmp_root, notes=str(notes_repo))
    shutil.rmtree(notes_repo)
    op = ops.setup(tmp_root)
    step = _step(op)
    assert step["state"] == "failed" and "is gone" in step["detail"]
    assert not notes_repo.exists(), "an empty repo in its place would hide the real notes"


def test_setup_check_configures_nothing(tmp_root: Path, notes_repo: Path, tools):
    op = ops.setup(tmp_root, check_only=True, notes=str(notes_repo))
    assert op.data["configure"] == []
    assert not (tmp_root / "features").exists()
    assert not (tmp_root / notes.CODEX_CONFIG_FILE).exists()


def test_codex_config_keeps_other_project_settings_and_updates_notes_path(tmp_root: Path):
    config_path = tmp_root / notes.CODEX_CONFIG_FILE
    config_path.parent.mkdir()
    config_path.write_text('model_reasoning_effort = "high"\n', encoding="utf-8")
    first = tmp_root.parent / "first-notes"
    second = tmp_root.parent / "second-notes"
    assert notes.configure_codex_notes_root(tmp_root, first)[0] == "done"
    assert notes.configure_codex_notes_root(tmp_root, first)[0] == "already"
    assert notes.configure_codex_notes_root(tmp_root, second)[0] == "done"
    content = config_path.read_text(encoding="utf-8")
    parsed = tomllib.loads(content)
    assert parsed["model_reasoning_effort"] == "high"
    assert parsed["permissions"]["agent-manager"]["filesystem"][str(second.resolve())] == "write"
    assert content.count("# BEGIN agent setup: Codex manager profile") == 1


def test_codex_config_preserves_reviewer_opt_out(tmp_root: Path):
    config_path = tmp_root / notes.CODEX_CONFIG_FILE
    config_path.parent.mkdir()
    config_path.write_text('approvals_reviewer = "user"\n', encoding="utf-8")
    assert notes.configure_codex_notes_root(tmp_root, tmp_root.parent / "notes")[0] == "done"
    content = config_path.read_text(encoding="utf-8")
    parsed = tomllib.loads(content)
    assert parsed["approvals_reviewer"] == "user"
    assert content.count("approvals_reviewer") == 1
    assert notes.configure_codex_notes_root(tmp_root, tmp_root.parent / "notes")[0] == "already"


def test_codex_config_migrates_the_old_generated_notes_block(tmp_root: Path):
    config_path = tmp_root / notes.CODEX_CONFIG_FILE
    config_path.parent.mkdir()
    config_path.write_text(
        "# BEGIN agent setup: notes writable root\n"
        "[sandbox_workspace_write]\n"
        'writable_roots = ["/old/notes"]\n'
        "# END agent setup: notes writable root\n",
        encoding="utf-8",
    )
    target = tmp_root.parent / "current-notes"
    assert notes.configure_codex_notes_root(tmp_root, target)[0] == "done"
    content = config_path.read_text(encoding="utf-8")
    parsed = tomllib.loads(content)
    assert "sandbox_workspace_write" not in parsed
    assert parsed["permissions"]["agent-manager"]["filesystem"][str(target.resolve())] == "write"
    assert parsed["permissions"]["agent-manager"]["network"]["unix_sockets"]["/tmp/cc-socks"] == "allow"
    assert "notes writable root" not in content


def test_codex_config_refuses_an_existing_unmanaged_sandbox_setting(tmp_root: Path):
    config_path = tmp_root / notes.CODEX_CONFIG_FILE
    config_path.parent.mkdir()
    original = "[sandbox_workspace_write]\nnetwork_access = false\n"
    config_path.write_text(original, encoding="utf-8")
    state, detail = notes.configure_codex_notes_root(tmp_root, tmp_root.parent / "notes")
    assert state == "refused"
    assert "already manages" in detail
    assert config_path.read_text(encoding="utf-8") == original


# ------------------------------------------------------------------ the CLI surface


def test_notes_cli_link_status_and_init(tmp_root: Path, notes_repo: Path, run):
    r = run(["--json", "notes", "link", str(notes_repo)])
    assert r.code == 0, r.err
    assert {row["state"] for row in r.json} <= {"done", "skipped"}
    config = tomllib.loads((tmp_root / notes.CODEX_CONFIG_FILE).read_text(encoding="utf-8"))
    assert config["permissions"]["agent-manager"]["filesystem"][str(notes_repo.resolve())] == "write"

    s = run(["--json", "notes", "status"])
    assert s.code == 0, s.err
    states = {row["path"]: row["state"] for row in s.json}
    assert states["features"] == "linked"
    assert states["agent.yaml"] == "linked"

    fresh = tmp_root.parent / "brand-new-notes"
    i = run(["--json", "notes", "init", str(fresh)])
    assert i.code == 0, i.err
    assert (fresh / "README.md").exists() and (fresh / ".git").is_dir()


def test_notes_link_exits_non_zero_when_something_is_refused(tmp_root: Path, notes_repo: Path, run):
    (tmp_root / "features").mkdir()
    r = run(["notes", "link", str(notes_repo)])
    assert r.code == 1
    assert "refused" in r.out
    assert "nothing was deleted" in r.out


# ------------------------------------------------------------------ doctor


def _checks(r) -> dict[str, dict]:
    return {c["check"]: c for c in r.json}


def test_doctor_has_a_row_per_private_path_in_each_state(tmp_root: Path, notes_repo: Path, run):
    run(["notes", "link", str(notes_repo)])
    run(["status", "render"])
    shutil.rmtree(notes_repo / "journal")  # a link whose target went away
    r = run(["--json", "doctor"])
    checks = _checks(r)
    assert checks["private features"]["level"] == "ok"
    assert "linked ->" in checks["private features"]["detail"]
    assert checks["private journal"]["level"] == "fail"
    assert "target is gone" in checks["private journal"]["detail"]
    assert r.code == 1, "a broken private link fails doctor"


def test_notes_root_names_the_repo_a_private_path_commits_in(tmp_root: Path, notes_repo: Path, run):
    """`git add journal/...` from the vault root is "beyond a symbolic link" once the set is
    linked, and the path is gitignored here besides. /journal asks this command instead."""
    plain = run(["--json", "notes", "root"])
    assert plain.code == 0
    assert plain.json["root"] == str(tmp_root.resolve()), "plain files commit where they sit"
    assert plain.json["linked"] is False

    run(["notes", "link", str(notes_repo)])
    linked = run(["--json", "notes", "root"])
    assert linked.json["root"] == str(notes_repo.resolve())
    assert linked.json["linked"] is True
    assert run(["notes", "root"]).out.strip() == str(notes_repo.resolve()), "one plain line"


def test_doctor_fails_on_plain_private_files(tmp_root: Path, notes_remote: Path, run):
    """A plain private file in the checkout is what breaks pulling the owner's
    updates, so it fails doctor and names the fix."""
    run(["feature", "new", "x", "--title", "X"])
    r = run(["--json", "doctor"])
    checks = _checks(r)
    assert checks["private features"]["level"] == "fail"
    assert "into the notes repo" in checks["private features"]["detail"]
    assert checks["private agent.yaml"]["level"] == "fail"
    assert checks["private .mcp.json"]["level"] == "warn", "missing, but only the owner's to add"
    assert r.code == 1


def test_doctor_fails_on_a_missing_registry_and_warns_on_missing_generated_files(tmp_root: Path, run):
    (tmp_root / "agent.yaml").unlink()
    r = run(["--json", "doctor"])
    checks = _checks(r)
    assert checks["private agent.yaml"]["level"] == "fail"
    assert checks["agent.yaml"]["level"] == "fail"
    assert "agent.example.yaml" in checks["agent.yaml"]["detail"]
    assert checks["private bases"]["level"] == "warn"
    assert "agent status render" in checks["private bases"]["detail"]


def test_doctor_notes_repo_row(tmp_root: Path, notes_repo: Path, run):
    missing = _checks(run(["--json", "doctor"]))["notes repo"]
    assert missing["level"] == "fail", "a notes repo is required"
    assert "Agent-notes" in missing["detail"]

    run(["notes", "link", str(notes_repo)])
    not_git = _checks(run(["--json", "doctor"]))["notes repo"]
    assert not_git["level"] == "ok" and str(notes_repo) in not_git["detail"]

    (notes_repo / ".git").mkdir()
    ok = _checks(run(["--json", "doctor"]))["notes repo"]
    assert ok["level"] == "ok" and str(notes_repo) in ok["detail"]


def test_doctor_enabled_servers_row_merges_the_local_settings(tmp_root: Path, run):
    (tmp_root / ".mcp.json").write_text('{"mcpServers":{"agent":{},"vendor":{}}}', encoding="utf-8")
    (tmp_root / ".claude").mkdir(exist_ok=True)
    (tmp_root / ".claude" / "settings.json").write_text(
        '{"enabledMcpjsonServers":["agent"]}', encoding="utf-8"
    )
    rows = [c for c in run(["--json", "doctor"]).json if c["check"] == "mcp servers"]
    [warn] = [r for r in rows if "enabledMcpjsonServers" in r["detail"]]
    assert warn["level"] == "warn" and "vendor" in warn["detail"]
    assert "settings.local.json" in warn["detail"]

    (tmp_root / ".claude" / "settings.local.json").write_text(
        '{"enabledMcpjsonServers":["vendor"]}', encoding="utf-8"
    )
    rows = [c for c in run(["--json", "doctor"]).json if c["check"] == "mcp servers"]
    assert not any("enabledMcpjsonServers" in r["detail"] for r in rows), rows
