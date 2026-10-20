"""`agent setup` for a coworker: a fresh install is a `consumer`, either client alone is enough,
and a `git pull` keeps the install. Uses `test_setup`'s fixtures, so nothing touches the machine.
"""

from __future__ import annotations

import io
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_cli import harness_role, ops
from agent_cli import setup as setup_mod
from harness_root import git
from test_setup import ALL_TOOLS, _notes_linked, _row, _which, no_shelling, quiet_doctor, tools  # noqa: F401


def _steps(data) -> dict[str, str]:
    return {s["step"]: s["state"] for s in data["configure"]}


def test_a_fresh_install_is_a_consumer(tmp_root: Path, tools, no_shelling, quiet_doctor, plugin_builds):
    (tmp_root / "agent.local.yaml").unlink()
    assert _steps(ops.setup(tmp_root).data)["harness role"] == "done"
    assert harness_role.role(tmp_root) == harness_role.CONSUMER


def test_an_existing_local_config_gains_no_harness_block(
    tmp_root: Path, tools, no_shelling, quiet_doctor, plugin_builds
):
    before = (tmp_root / "agent.local.yaml").read_bytes()
    assert _steps(ops.setup(tmp_root).data)["harness role"] == "already"
    assert (tmp_root / "agent.local.yaml").read_bytes() == before
    assert harness_role.role(tmp_root) == harness_role.SOURCE


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.mark.parametrize("absent", ["claude", "codex"])
@pytest.mark.parametrize("answer", ["ask", "--yes"])
def test_one_client_is_enough(
    tmp_root: Path, tools, no_shelling, plugin_builds, run, monkeypatch: pytest.MonkeyPatch, absent, answer
):
    """jq is missing too, so there is an offer to make; it never names the absent client."""
    present = set(ALL_TOOLS) - {absent, "jq"} | {"brew"}
    monkeypatch.setattr(
        "shutil.which", lambda cmd, *a, **k: f"/usr/local/bin/{cmd}" if cmd in present else None
    )
    ran: list[list[str]] = []
    real_run = setup_mod.subprocess.run

    def fake_run(argv, **kwargs):
        if argv[0] not in ("brew", "npm"):
            return real_run(argv, **kwargs)
        ran.append(argv)
        present.add(argv[-1])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(setup_mod.subprocess, "run", fake_run)
    prompts: list[str] = []
    monkeypatch.setattr("builtins.input", lambda p="": prompts.append(p) or ("y" if "Install" in p else ""))
    if answer == "ask":
        monkeypatch.setattr("sys.stdin", _Tty())
        r = run(["--json", "setup"])
        assert [p for p in prompts if "Install" in p] == ["Missing: jq. Install them now? [Y/n] "]
    else:
        r = run(["--json", "setup", "--yes"], stdin="")
    assert ran == [["brew", "install", "jq"]]
    assert r.code == 0, r.out
    skipped = {"claude": ["claude worktree grant"], "codex": ["codex manager profile", "codex agent MCP"]}
    assert all(_steps(r.json)[step] == "skipped" for step in skipped[absent])
    assert (tmp_root / ".codex" / "config.toml").exists() is (absent == "claude")
    assert setup_mod.next_steps(r.json)[0] == "Setup is complete."


def test_neither_client_names_both(tmp_root: Path, tools, no_shelling, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("shutil.which", _which(set(ALL_TOOLS) - {"claude", "codex"}))
    op = ops.setup(tmp_root)
    assert op.code == 1
    assert op.data["missing"] == ["claude", "codex"]
    assert all(_row(op.data, name)["guide"] in op.text for name in ("claude", "codex"))
    assert "One client is enough" in op.text


def test_a_pull_keeps_notes_config_and_client_wiring(
    tmp_root: Path, tmp_path: Path, tools, no_shelling, quiet_doctor, plugin_builds
):
    """An upstream seeded from the fixture root's tracked files, cloned fresh, set up, then
    fast-forwarded to a new upstream commit. The clone starts with no notes: setup links them."""
    from agent_cli import links
    from agent_cli import notes as notes_mod

    upstream, other = tmp_path / "upstream.git", tmp_path / "other"
    clone = tmp_path / "fresh" / "harness"
    who = ["-c", "user.name=Owner", "-c", "user.email=owner@example.com"]
    git(["init", "--bare", "-q", "--initial-branch=main", str(upstream)], tmp_path)
    repo = Path(__file__).resolve().parent.parent
    (tmp_root / ".claude").mkdir()
    for tracked in (".gitignore", ".claude/settings.json"):
        shutil.copy(repo / tracked, tmp_root / tracked)
    (tmp_root / "agent.example.yaml").write_text((tmp_root / "agent.yaml").read_text(encoding="utf-8"))
    git(["init", "-q", "--initial-branch=main"], tmp_root)
    git(["add", "-A"], tmp_root)
    git([*who, "commit", "-q", "-m", "harness"], tmp_root)
    git(["push", "-q", str(upstream), "main"], tmp_root)
    clone.parent.mkdir()
    git(["clone", "-q", str(upstream), str(clone)], tmp_path)
    paths = [clone / e.path for e in notes_mod.PRIVATE_SET]
    assert not any(p.exists() or links.is_link(p) for p in paths), "the clone starts with no notes"
    assert not (clone / "agent.local.yaml").exists()
    assert _steps(ops.setup(clone).data)["notes link"] == "done"

    def install() -> tuple[dict, dict]:
        notes = {p: links.link_target(p) for p in paths if links.is_link(p)}
        assert notes[clone / "features"] == clone.parent / notes_mod.DEFAULT_NOTES_DIRNAME / "features"
        wiring = ("agent.local.yaml", ".codex/config.toml", ".claude/settings.local.json")
        return notes, {p: (clone / p).read_bytes() for p in wiring}

    before = install()
    git(["clone", "-q", str(upstream), str(other)], tmp_path)
    (other / "CHANGELOG.md").write_text("an upstream change\n", encoding="utf-8")
    git(["add", "CHANGELOG.md"], other)
    git([*who, "commit", "-q", "-m", "upstream"], other)
    git(["push", "-q", "origin", "main"], other)
    git(["pull", "-q", "--ff-only"], clone)
    assert (clone / "CHANGELOG.md").is_file()
    assert install() == before
    assert harness_role.role(clone) == harness_role.CONSUMER
