"""`agent setup`: the requirement checks, the guides, the configure steps, idempotence.

Nothing here touches the machine. `shutil.which` is patched to a fixed set of tools, the two
steps that shell out (`uv sync`, the caveman submodule) are patched to recorders, and the
platform is patched when the OS is what is under test.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agent_cli import installs as installs_mod
from agent_cli import obsidian as obsidian_mod
from agent_cli import ops
from agent_cli import setup as setup_mod
from harness_root import link_notes

ALL_TOOLS: tuple[str, ...] = ("git", "uv", "node", "claude", "codex", "gh", "jq", "obsidian")


def _which(present: set[str]):
    def fake(cmd: str, *args, **kwargs):
        return f"/usr/local/bin/{cmd}" if cmd in present else None

    return fake


@pytest.fixture(autouse=True)
def _notes_linked(tmp_root: Path):
    """A real clone after setup has its private set linked from a notes repo."""
    link_notes(tmp_root)


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch):
    """Everything present, signed in to gh, on macOS. A test narrows it from there."""
    monkeypatch.setattr("shutil.which", _which(set(ALL_TOOLS)))
    # The Obsidian CLI resolver also checks $OBSIDIAN_CLI and the macOS install spots; without
    # this a developer's real CLI would answer for a test that says it is absent.
    monkeypatch.delenv("OBSIDIAN_CLI", raising=False)
    monkeypatch.setattr(obsidian_mod, "CLI_FALLBACKS", ())
    monkeypatch.setattr(obsidian_mod, "cli_version", lambda exe: (None, "unverified: app closed"))
    monkeypatch.setattr(setup_mod, "run_ok", lambda *a, **kw: True)
    monkeypatch.setattr(setup_mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(installs_mod, "obsidian_app_found", lambda system: "/Applications/Obsidian.app")
    return monkeypatch


@pytest.fixture
def no_shelling(monkeypatch: pytest.MonkeyPatch):
    """The two configure steps that run a real command, as recorders."""
    calls: list[str] = []

    def uv_sync(root: Path):
        calls.append("uv sync")
        return {"step": "uv sync", "state": "done", "detail": ""}

    def caveman(root: Path):
        calls.append("caveman submodule")
        return {"step": "caveman submodule", "state": "done", "detail": ""}

    def terminal_python(root: Path, system: str | None = None):
        calls.append("terminal python")
        return {"step": "terminal python", "state": "already", "detail": ""}

    monkeypatch.setattr(setup_mod, "uv_sync", uv_sync)
    monkeypatch.setattr(setup_mod, "caveman_submodule", caveman)
    monkeypatch.setattr(installs_mod, "terminal_python", terminal_python)
    return calls


@pytest.fixture
def quiet_doctor(monkeypatch: pytest.MonkeyPatch):
    """doctor's rows are appended to the setup report. The real one is `test_doctor`'s job."""
    from agent_cli.ops import infra as ops_infra

    calls: list[Path] = []

    def stub(root: Path):
        calls.append(root)
        return ops.Op([{"check": "features", "level": "ok", "detail": "1 valid"}], "")

    monkeypatch.setattr(ops_infra, "doctor", stub)
    yield calls
    assert calls, "setup never called the doctor stub"


def _names(result) -> list[str]:
    return [row["name"] for row in result["checks"]]


def _row(result, name: str) -> dict:
    return next(row for row in result["checks"] if row["name"] == name)


def test_check_only_reports_every_requirement(tmp_root: Path, tools):
    op = ops.setup(tmp_root, check_only=True)
    assert op.code == 0
    assert _names(op.data) == [
        "git",
        "uv",
        "python",
        "node",
        "claude",
        "codex",
        "gh",
        "gh auth",
        "jq",
        "obsidian",
        "obsidian app",
    ]
    assert all(row["ok"] for row in op.data["checks"])
    assert op.data["configure"] == [], "--check configures nothing"
    assert op.data["doctor"] == []
    assert _row(op.data, "python")["found"].startswith("3.")


def test_all_present_runs_every_configure_step(
    tmp_root: Path, tools, no_shelling, quiet_doctor, plugin_builds
):
    op = ops.setup(tmp_root)
    assert op.code == 0, op.text
    assert no_shelling == ["uv sync", "caveman submodule", "terminal python"]
    assert [s["step"] for s in op.data["configure"]] == [
        "uv sync",
        "caveman submodule",
        "skill links",
        "terminal python",
        "git hooks",
        "harness role",
        "machine label",
        "notes link",
        "codex manager profile",
        "codex agent MCP",
        "claude worktree grant",
        "obsidian setup",
        "status render",
    ]
    assert (tmp_root / ".obsidian" / "app.json").exists(), "obsidian setup really ran"
    assert "[mcp_servers.agent]" in (tmp_root / ".codex" / "config.toml").read_text()
    assert (tmp_root / "Dashboard.md").exists(), "status render really ran"
    assert op.data["doctor"][0]["check"] == "features", "doctor's rows are appended"
    assert "features" in op.text
    assert op.data["ok"] is True, op.data["configure"]


def test_missing_hard_requirement_exits_one_with_the_install_line(
    tmp_root: Path, tools, no_shelling, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("shutil.which", _which(set(ALL_TOOLS) - {"git"}))
    op = ops.setup(tmp_root)
    assert op.code == 1
    assert op.data["missing"] == ["git"]
    assert _row(op.data, "git")["guide"] == "brew install git  https://git-scm.com/downloads"
    assert "brew install git" in op.text
    assert "missing hard requirement(s): git" in op.text
    assert op.data["configure"] == [], "nothing is configured while a requirement is missing"
    assert no_shelling == []


def test_the_guide_is_per_os(tools, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("shutil.which", _which(set()))
    guides = {}
    for system in ("Darwin", "Linux", "Windows"):
        rows = setup_mod.checks(system)
        guides[system] = next(r for r in rows if r["name"] == "node")["guide"]
    assert guides["Darwin"].startswith("brew install node")
    assert guides["Linux"].startswith("sudo apt install nodejs")
    assert "dnf" in guides["Linux"]
    assert guides["Windows"].startswith("winget install --id OpenJS.NodeJS.LTS")
    assert all("https://nodejs.org" in g for g in guides.values())


def test_an_optional_tool_missing_is_not_a_failure(tmp_root: Path, tools, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("shutil.which", _which(set(ALL_TOOLS) - {"jq"}))
    op = ops.setup(tmp_root, check_only=True)
    assert op.code == 0
    assert op.data["missing"] == []
    assert _row(op.data, "jq")["ok"] is False
    assert "jq (optional)" in op.text


def test_a_missing_obsidian_cli_makes_setup_incomplete(
    tmp_root: Path, tools, no_shelling, quiet_doctor, plugin_builds, monkeypatch: pytest.MonkeyPatch
):
    """The owner made the Obsidian CLI required on 2026-09-26: tab titles, state glyphs and a
    manager's view of its tabs all run through it."""
    monkeypatch.setattr("shutil.which", _which(set(ALL_TOOLS) - {"obsidian"}))
    op = ops.setup(tmp_root)
    assert op.data["incomplete"] == ["obsidian"]
    assert op.code == 1
    assert "obsidian (optional)" not in op.text
    assert "tabs get no titles" in _row(op.data, "obsidian")["guide"]
    assert "Still missing: obsidian." in op.text and setup_mod.TOOLS[-1].macos in op.text


def test_a_headless_runner_is_incomplete_for_the_cli_then_the_app(
    tmp_root: Path, tools, no_shelling, quiet_doctor, plugin_builds, monkeypatch: pytest.MonkeyPatch
):
    """The exact list the windows-setup CI job expects, in this order (tests.yml)."""
    monkeypatch.setattr("shutil.which", _which(set(ALL_TOOLS) - {"obsidian"}))
    monkeypatch.setattr(installs_mod, "obsidian_app_found", lambda system: "")
    assert ops.setup(tmp_root).data["incomplete"] == ["obsidian", "obsidian app"]


def test_gh_auth_is_its_own_row(tmp_root: Path, tools, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(setup_mod, "run_ok", lambda *a, **kw: False)
    op = ops.setup(tmp_root, check_only=True)
    assert op.code == 0, "gh is optional, so an unauthenticated gh does not stop setup"
    assert _row(op.data, "gh auth") == {
        "name": "gh auth",
        "ok": False,
        "found": "not signed in",
        "guide": "gh auth login",
    }


def test_gh_auth_row_is_absent_when_gh_is(tmp_root: Path, tools, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("shutil.which", _which(set(ALL_TOOLS) - {"gh"}))
    op = ops.setup(tmp_root, check_only=True)
    assert "gh auth" not in _names(op.data)


def test_windows_report_has_no_obsolete_caveats(
    tmp_root: Path, tools, no_shelling, quiet_doctor, plugin_builds, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(setup_mod.platform, "system", lambda: "Windows")
    op = ops.setup(tmp_root)
    assert op.code == 0
    assert op.data["platform"] == "Windows"
    text = op.text
    assert "Windows: this harness is not there yet." not in text
    assert "caveats" not in op.data
    assert no_shelling == ["uv sync", "caveman submodule", "terminal python"], "the configure steps still run"


def test_one_yes_installs_the_missing_tools_in_order_then_rechecks(
    tmp_root: Path, tools, no_shelling, quiet_doctor, plugin_builds, monkeypatch: pytest.MonkeyPatch
):
    """After the owner's yes, setup installs what it can, node before claude
    (claude installs through npm), then checks again so the report sees what is there now."""
    present = set(ALL_TOOLS) - {"node", "claude", "codex"} | {"brew"}
    monkeypatch.setattr(
        "shutil.which", lambda cmd, *a, **k: f"/usr/local/bin/{cmd}" if cmd in present else None
    )
    ran: list[list[str]] = []

    class Proc:
        returncode = 0
        stdout = stderr = ""

    real_run = setup_mod.subprocess.run

    def fake_run(argv, **kwargs):
        if argv[0] not in ("brew", "npm"):
            return real_run(argv, **kwargs)
        ran.append(argv)
        present.add({"brew": "node", "npm": "claude"}[argv[0]])
        if argv[0] == "brew":
            present.add("npm")
        return Proc()

    monkeypatch.setattr(setup_mod.subprocess, "run", fake_run)
    op = ops.setup(tmp_root, install=True)
    assert ran == [["brew", "install", "node"], ["npm", "install", "-g", "@anthropic-ai/claude-code"]]
    assert [r["state"] for r in op.data["installed"]] == ["installed", "installed"]
    assert op.data["missing"] == [], "the recheck sees node and claude"
    assert op.code == 0, op.text


def test_without_the_yes_nothing_installs(
    tmp_root: Path, tools, no_shelling, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("shutil.which", _which(set(ALL_TOOLS) - {"node"} | {"brew"}))
    monkeypatch.setattr(
        installs_mod, "install_missing", lambda *a, **k: pytest.fail("installed without a yes")
    )
    op = ops.setup(tmp_root)
    assert op.code == 1
    assert op.data["installed"] == []
    assert op.data["missing"] == ["node"]


def test_a_missing_obsidian_app_configures_but_is_incomplete(
    tmp_root: Path, tools, no_shelling, quiet_doctor, plugin_builds, monkeypatch: pytest.MonkeyPatch
):
    """The vault is still configured without the app, but setup exits nonzero and says so,
    with the exact install line."""
    monkeypatch.setattr(installs_mod, "obsidian_app_found", lambda system: "")
    op = ops.setup(tmp_root)
    assert op.data["configure"], "configuring still ran"
    assert op.data["incomplete"] == ["obsidian app"]
    assert op.code == 1
    assert "Setup is incomplete." in op.text
    assert "brew install --cask obsidian" in _row(op.data, "obsidian app")["guide"]
    assert "Still missing: obsidian app." in op.text and setup_mod.OBSIDIAN_APP.macos in op.text


def test_a_complete_run_says_complete(tmp_root: Path, tools, no_shelling, quiet_doctor, plugin_builds):
    """Setup used to count every step as needing attention and never said it was complete."""
    op = ops.setup(tmp_root)
    assert "Setup is complete." in op.text, op.text


def test_a_doctor_failure_is_not_called_complete(
    tmp_root: Path, tools, no_shelling, monkeypatch: pytest.MonkeyPatch
):
    from agent_cli.ops import infra as ops_infra

    monkeypatch.setattr(
        ops_infra,
        "doctor",
        lambda root: ops.Op([{"check": "skill links", "level": "fail", "detail": "x"}], "", code=1),
    )
    op = ops.setup(tmp_root)
    assert "Setup is complete." not in op.text
    assert "Doctor failures: skill links" in op.text
    assert op.code == 1


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="setup.sh hands a Windows shell to setup.ps1, so its bash --check path never runs",
)
def test_check_mode_never_syncs_a_fresh_clone(tmp_path: Path):
    """`setup.sh --check` writes nothing: on a clone never synced it says so and creates no
    .venv, instead of running `uv sync`."""
    import shutil
    import subprocess

    src = Path(__file__).resolve().parent.parent / "setup.sh"
    shutil.copy(src, tmp_path / "setup.sh")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    uv = fake_bin / "uv"
    uv.write_text('#!/bin/sh\necho uv called "$@" >> "$(dirname "$0")/calls"\n')
    uv.chmod(0o755)
    env = {"PATH": f"{fake_bin}:/usr/bin:/bin", "HOME": str(tmp_path)}
    proc = subprocess.run(
        ["bash", str(tmp_path / "setup.sh"), "--check"], capture_output=True, text=True, env=env
    )
    assert proc.returncode == 1
    assert "not synced yet" in proc.stdout
    assert not (tmp_path / ".venv").exists()
    assert not (fake_bin / "calls").exists(), "uv was not run at all"


def test_the_terminal_python_is_windows_only(tmp_root: Path):
    assert installs_mod.terminal_python(tmp_root, "Darwin")["state"] == "already"
    assert installs_mod.terminal_python_path(tmp_root, "Windows").parts[-2:] == ("Scripts", "python.exe")
    assert installs_mod.terminal_python_path(tmp_root, "Linux").parts[-2:] == ("bin", "python")


def test_setup_sh_hands_windows_shells_to_powershell():
    """Git Bash on Windows runs setup.ps1, so the Windows paths are the ones that run."""
    import subprocess

    root = Path(__file__).resolve().parent.parent
    assert subprocess.run(["bash", "-n", str(root / "setup.sh")]).returncode == 0
    text = (root / "setup.sh").read_text()
    assert "MINGW* | MSYS* | CYGWIN*" in text
    assert "-ExecutionPolicy Bypass -File" in text
    assert "--ExecutionPolicy" not in (root / "setup.cmd").read_text()
    assert "-ExecutionPolicy Bypass" in (root / "setup.cmd").read_text()


def test_line_ending_catch_all_is_first_so_the_crlf_pins_win():
    """`* text=auto eol=lf` checks Python out as LF under Windows autocrlf. A
    later attribute line wins, so appending it flipped `setup.cmd` to LF and broke cmd.exe."""
    import subprocess

    root = Path(__file__).resolve().parent.parent
    assert (root / ".gitattributes").read_text().splitlines()[0] == "* text=auto eol=lf"
    paths = ["setup.cmd", "setup.ps1", "bin/agent.cmd", "setup.sh", "src/agent_cli/core.py"]
    out = subprocess.run(
        ["git", "check-attr", "eol", "--", *paths], cwd=root, capture_output=True, text=True, check=True
    ).stdout
    eol = {line.split(": ")[0]: line.rsplit(": ", 1)[1] for line in out.splitlines()}
    assert eol == {
        "setup.cmd": "crlf",
        "setup.ps1": "crlf",
        "bin/agent.cmd": "crlf",
        "setup.sh": "lf",
        "src/agent_cli/core.py": "lf",
    }


def test_second_run_reports_already_configured(
    tmp_root: Path, tools, no_shelling, quiet_doctor, plugin_builds
):
    first = ops.setup(tmp_root)
    assert {s["step"]: s["state"] for s in first.data["configure"]}["obsidian setup"] == "done"
    assert {s["step"]: s["state"] for s in first.data["configure"]}["status render"] == "done"
    second = ops.setup(tmp_root)
    assert second.code == 0
    states = {s["step"]: s["state"] for s in second.data["configure"]}
    assert states["obsidian setup"] == "already"
    assert states["status render"] == "already"
    assert "already" in second.text


def test_cli_setup_check(tmp_root: Path, tools, run):
    r = run(["--json", "setup", "--check"])
    assert r.code == 0, r.err
    assert r.json["platform"] == "Darwin"
    assert r.json["configure"] == []


def test_cli_setup_exits_one_on_a_missing_requirement(
    tmp_root: Path, tools, run, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("shutil.which", _which(set(ALL_TOOLS) - {"uv"}))
    r = run(["setup", "--check"])
    assert r.code == 1
    assert "brew install uv" in r.out


def test_caveman_submodule_is_skipped_when_already_sparse(tmp_root: Path, monkeypatch: pytest.MonkeyPatch):
    ran: list[list[str]] = []
    monkeypatch.setattr(setup_mod, "run_ok", lambda argv, cwd=None: ran.append(argv) or True)
    sub = tmp_root / setup_mod.VENDOR_CAVEMAN
    (sub / "skills").mkdir(parents=True)
    (sub / "skills" / "keep.md").write_text("x", encoding="utf-8")
    assert setup_mod.caveman_submodule(tmp_root) == {
        "step": "caveman submodule",
        "state": "already",
        "detail": "initialised and sparse",
    }
    assert ran == []
    (sub / "CLAUDE.md").write_text("a full checkout", encoding="utf-8")
    assert setup_mod.caveman_submodule(tmp_root)["state"] == "done"
    assert ran == [setup_mod.CAVEMAN_SPARSE.split()], "populated already, so only the sparse set runs"


def test_uv_sync_reports_already_when_nothing_changed(tmp_root: Path, monkeypatch: pytest.MonkeyPatch):
    class Proc:
        returncode = 0
        stdout = ""
        stderr = "Resolved 40 packages\nAudited 38 packages\n"

    monkeypatch.setattr(setup_mod.subprocess, "run", lambda *a, **kw: Proc())
    assert setup_mod.uv_sync(tmp_root)["state"] == "already"
    Proc.stderr = "Resolved 40 packages\nInstalled 2 packages\n"
    assert setup_mod.uv_sync(tmp_root)["state"] == "done"


def test_uv_sync_failure_is_a_failed_step(tmp_root: Path, monkeypatch: pytest.MonkeyPatch):
    class Proc:
        returncode = 2
        stdout = ""
        stderr = "error: no interpreter found for python 3.14\n"

    monkeypatch.setattr(setup_mod.subprocess, "run", lambda *a, **kw: Proc())
    step = setup_mod.uv_sync(tmp_root)
    assert step["state"] == "failed"
    assert "no interpreter" in step["detail"]


def test_setup_ends_on_what_to_do_next():
    """Setup used to end on a table and say nothing. `result["ok"]` was computed and never
    printed, so a newcomer who did everything right reached the end of the output with no idea
    whether it had worked or what came next — and that screen is the harness's whole first
    impression."""
    from agent_cli.setup import next_steps

    done = {"missing": [], "configure": [], "ok": True, "owner_name": "Ada", "repo_names": ["web-api"]}
    lines = next_steps(done)
    assert lines[0] == "Setup is complete."
    joined = "\n".join(lines)
    assert "/intake" in joined
    # already configured: do not tell someone to do what they have done, or the list stops
    # being read
    assert "agent.yaml" not in joined
    # the one thing a fresh clone is always surprised by
    assert "terse by default" in joined and "/output-style default" in joined


def test_next_steps_names_only_what_is_actually_unset():
    """The two things a fresh clone always needs are a real name and a real repo, and the
    tracked example config has neither. Numbering is computed: with the owner already set and
    only the example repo left, hardcoded numbers printed "2." twice and no "1.".
    """
    from agent_cli.setup import next_steps

    fresh = {
        "missing": [],
        "configure": [],
        "owner_name": "",  # repos.owner_name reads the example's "Your Name" as unset
        "repo_names": ["example-service"],
    }
    lines = next_steps(fresh)
    assert [line.strip()[:2] for line in lines if line.startswith("  ")] == ["1.", "2.", "3.", "4."]
    assert "Put your name" in lines[1] and "example-service" in lines[2]

    half = dict(fresh, owner_name="Ada")
    numbered = [line for line in next_steps(half) if line.startswith("  ")]
    assert [line.strip()[:2] for line in numbered] == ["1.", "2.", "3."]
    assert "example-service" in numbered[0]


def test_codex_hook_trust_is_always_a_numbered_step():
    """A reclone at the same path inherits the old clone's Codex trust, and Codex skips changed
    hooks silently. Doctor's trust row is gone, so setup always names /hooks."""
    from agent_cli.setup import next_steps

    base = {"missing": [], "configure": [], "owner_name": "Ada", "repo_names": []}
    assert any("/hooks" in line for line in next_steps(base))


def test_a_missing_requirement_says_install_first_and_nothing_else():
    """Configuring against a missing tool reports worse errors than the plain list does, so
    setup stops — and the next-steps block must not then tell someone to run `claude`."""
    from agent_cli.setup import next_steps

    lines = next_steps({"missing": ["git"], "configure": []})
    joined = "\n".join(lines)
    assert "install the missing requirement" in joined
    assert "/intake" not in joined
