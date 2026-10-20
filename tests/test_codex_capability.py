"""A Codex launch is refused before allocation when its work order asks for what a Codex worker
cannot do, and the refusal names the supported route (phase C)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from agent_cli import links
from agent_cli.core import AgentError
from agent_cli.dispatch import codex_capability
from dispatch_helpers import _new_fast, _set_alpha_dispatch, droot  # noqa: F401 -- pytest fixture


def _repo(**dispatch: Any) -> Any:
    return SimpleNamespace(dispatch=dispatch)


def _rows(*commands: str) -> list[dict[str, Any]]:
    return [{"command": c, "verdict": "allowed", "source": "work-order"} for c in commands]


def _sentences(d: dict[str, Any], rows: list[dict[str, Any]], **dispatch: Any) -> list[str]:
    extra = codex_capability.check(d, _repo(**dispatch), rows)
    return [r["warning"] for r in [*rows, *extra] if r.get("warning")]


@pytest.mark.parametrize(
    ("command", "phrase"),
    [
        ("git merge --ff-only agent/001-mac-09", "writes Git metadata"),
        ("git rebase main", "writes Git metadata"),
        ("git checkout main", "writes Git metadata"),
        ("git stash", "writes Git metadata"),
        ("git fetch origin", "needs the network"),
        ("gh api repos/x/y", "needs the network"),
        ("uv sync", "needs the network"),
        ("uv run python -c 'import x'", "use `.venv/bin/python`"),
        ("git restore --staged -- src/file", "writes Git metadata"),
        ("git restore --source HEAD~1 -- src/file", "writes Git metadata"),
        ("git restore src/file", "writes Git metadata"),
        ("git branch new-branch", "writes Git metadata"),
        ("git branch -D old", "writes Git metadata"),
        ("git branch --set-upstream-to=origin/main", "writes Git metadata"),
        ("git branch --unset-upstream", "writes Git metadata"),
        ("git branch -u origin/main", "writes Git metadata"),
        ("git branch --unset-ups", "writes Git metadata"),  # git expands abbreviations
        ("git branch --edit-desc", "writes Git metadata"),
        ("git branch --show-cur", "writes Git metadata"),  # unlisted, so conservatively a write
        ("git remote set-url origin https://example.com/repo", "writes Git metadata"),
        ("git remote show origin", "needs the network"),
    ],
)
def test_unsupported_codex_operations_are_named_with_a_route(command: str, phrase: str) -> None:
    sentences = _sentences({"provider": "codex"}, _rows(command))
    assert len(sentences) == 1 and phrase in sentences[0]
    assert "Claude worker" in sentences[0] or ".venv/bin/python" in sentences[0]


@pytest.mark.parametrize(
    "command",
    [
        "git add src/x.py",
        "git commit -m fix",
        "git restore -- src/x.py",
        "git checkout -- src/x.py",
        "git diff main",
        "uv run pytest -n 0 tests/test_x.py",
        ".venv/bin/python -m pytest tests/test_x.py",
        "odd-tool --version",
        "gh --version",
        "git branch --show-current",
        "git branch -a",
        "git branch --contains abc123",
        "git branch --list 'agent/*'",
        "git branch -a --format='%(refname)'",
        "git branch -r -v",
        "git restore --worktree -- src/x.py",
        "git remote show -n origin",
    ],
)
def test_supported_codex_operations_pass(command: str) -> None:
    assert _sentences({"provider": "codex"}, _rows(command)) == []


def test_the_network_grant_lifts_network_refusals_only() -> None:
    rows = _rows("git fetch origin", "git merge main")
    sentences = _sentences({"provider": "codex"}, rows, codex_network_access=True)
    assert len(sentences) == 1 and "Git metadata" in sentences[0]


def test_direct_writes_follow_scoped_permissions_and_resolve_symlinks(tmp_path: Path) -> None:
    from agent_cli.dispatch.codex_policy import policy_for

    worktree = tmp_path / "candidate"
    worktree.mkdir()
    main = tmp_path / "main"
    main.mkdir()
    protected = worktree / "settings.json"
    protected.write_text("protected")
    links.make(worktree / "escape", main, is_dir=True)
    d = {"provider": "codex", "worktree": str(worktree)}
    repo = _repo()
    policy = policy_for(
        d,
        repo,
        writable=[str(worktree)],
        permissions_profile="worker",
        filesystem_permissions=((str(main), "read"), (str(protected), "read")),
    )
    rows = _rows(
        "touch ordinary.txt",
        "touch settings.json",
        "touch escape/forged.json",
        f"cp settings.json {(main / 'copy.json').as_posix()}",
        "cp settings.json escape",
    )
    assert codex_capability.check(d, repo, rows, policy=policy) == []
    assert [r["verdict"] for r in rows] == [
        "allowed",
        "unsupported",
        "unsupported",
        "unsupported",
        "unsupported",
    ]
    assert "read-only restriction" in rows[1]["warning"]


@pytest.mark.parametrize("command", ["touch $TARGET", "cp -t output input", "touch *.json"])
def test_ambiguous_write_targets_remain_unverified(command: str) -> None:
    rows = _rows(command)
    codex_capability.check({"provider": "codex"}, _repo(), rows)
    assert rows[0]["verdict"] == "unverified"


@pytest.mark.parametrize(
    "command",
    [
        'python -c \'__import__("urllib.request", fromlist=["urlopen"]).urlopen("https://example.com")\'',
        ".venv/bin/python script.py",
        "python3 -m arbitrary_module",
        "node -e 'fetch(\"https://example.com\")'",
        "uv run custom-tool",
        "custom-tool run",
        "git -c core.fsmonitor=./candidate-script status",
        "git custom-subcommand",
        "git diff --output /elsewhere/result",
        "echo hi>/etc/outside",
        "echo hi >/etc/outside",
        "git log >/etc/x",
        "git status >/tmp/../etc/x",
        "ls $(curl https://x)",
        "echo $(curl https://x)",
    ],
)
@pytest.mark.parametrize("network", [False, True])
def test_opaque_effects_are_unverified_even_with_broad_rules_and_network(command: str, network: bool) -> None:
    from agent_cli.dispatch.preflight import check_preflight

    rows = _rows(command)
    assert codex_capability.check({"provider": "codex"}, _repo(codex_network_access=network), rows) == []
    assert rows[0]["verdict"] == "unverified"
    assert rows[0]["rule_verdict"] == "allowed"
    assert rows[0]["capability_verdict"] == "unverified"
    with pytest.raises(AgentError, match="opaque executable effects"):
        check_preflight(rows, allow_unmatched=False)
    check_preflight(rows, allow_unmatched=True)
    assert rows[0]["verdict"] == "unverified"


def test_protected_writes_route_to_claude() -> None:
    d = {"provider": "codex", "writes": [".agents/skills/dispatch/SKILL.md", "src/x.py"]}
    sentences = _sentences(d, [])
    assert len(sentences) == 1 and ".agents/skills/dispatch/SKILL.md" in sentences[0]


def test_setup_commands_run_harness_side_and_are_not_checked() -> None:
    """A replay: `dispatch.setup: [uv sync]` refused every Codex launch."""
    rows = [{"command": "uv sync", "verdict": "allowed", "source": "setup"}]
    assert _sentences({"provider": "codex"}, rows) == []
    assert _sentences({"provider": "codex"}, _rows("uv sync"))  # in the work order it is the worker's


def test_claude_launches_are_not_checked() -> None:
    d = {"provider": "claude", "writes": [".agents/skills/x.md"]}
    assert _sentences(d, _rows("git merge main", "gh api x")) == []


def test_mcp_profiles_on_codex_get_an_advisory_row() -> None:
    rows = codex_capability.check({"provider": "codex", "mcp": ["browser"]}, _repo(), [])
    assert [r["verdict"] for r in rows] == ["warning"]


def test_a_codex_launch_is_refused_before_allocation_and_previewed(droot: Path) -> None:
    prompt = "Edit the skill.\n\n```sh\ngit merge --ff-only main\n```\n"
    kw: dict[str, Any] = {
        "provider": "codex",
        "model": "gpt-6-luna",
        "prompt": prompt,
        "writes": [".agents/skills/dispatch/SKILL.md"],
    }
    preview = _new_fast(droot, dry_run=True, **kw)
    unsupported = [r for r in preview["preflight"] if r["verdict"] == "unsupported"]
    assert {r["command"] for r in unsupported} == {
        ".agents/skills/dispatch/SKILL.md",
        "git merge --ff-only main",
    }
    with pytest.raises(AgentError, match="preflight refused this launch") as caught:
        _new_fast(droot, **kw)
    assert "Claude worker" in str(caught.value)
    assert not list((droot / ".agent" / "dispatches").glob("*.json")) or all(
        "codex" not in p.read_text() for p in (droot / ".agent" / "dispatches").glob("*.json")
    )


def test_rule_allowed_network_read_is_refused_under_the_rendered_policy(droot: Path) -> None:
    prompt = "Read the repo.\n```sh\ngh api repos/x/y\n```\n"
    preview = _new_fast(droot, provider="codex", model="gpt-6-luna", prompt=prompt, dry_run=True)
    row = next(r for r in preview["preflight"] if r["command"] == "gh api repos/x/y")
    assert row["rule_verdict"] == "allowed"
    assert row["verdict"] == row["capability_verdict"] == "unsupported"
    assert preview["options"]["sandbox_policy"]["networkAccess"] is False
    with pytest.raises(AgentError, match="needs the network"):
        _new_fast(droot, provider="codex", model="gpt-6-luna", prompt=prompt)


GITLAB_READS = [
    "glab mr view 2 --repo example/project",
    "glab mr diff 2",
    "glab mr list --author @me",
    "glab api projects/1/merge_requests/2/notes",
]
GITLAB_WRITES = [
    "glab mr note 2 --message hi",
    "glab mr create --title x",
    "glab api -X POST projects/1/merge_requests",
    "glab api projects/1/notes -f body=hi",
]


def _gitlab_preview(droot: Path, network: bool, commands: list[str]) -> dict[str, dict[str, Any]]:
    _set_alpha_dispatch(droot, codex_network_access=network)
    data = yaml.safe_load((droot / "agent.yaml").read_text(encoding="utf-8"))
    next(r for r in data["repos"] if r["short"] == "alpha")["forge"] = "gitlab"
    (droot / "agent.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    prompt = "Read.\n```sh\n" + "\n".join(commands) + "\n```\n"
    preview = _new_fast(droot, provider="codex", model="gpt-6-luna", prompt=prompt, dry_run=True)
    return {r["command"]: r for r in preview["preflight"] if r["command"] in commands}


def test_gitlab_reads_are_supported_with_network_and_name_the_grant_without(droot: Path) -> None:
    on = _gitlab_preview(droot, True, GITLAB_READS)
    assert {c: r["capability_verdict"] for c, r in on.items()} == dict.fromkeys(GITLAB_READS, "supported")
    off = _gitlab_preview(droot, False, GITLAB_READS)
    for row in off.values():
        assert row["rule_verdict"] == "allowed"
        assert row["verdict"] == row["capability_verdict"] == "unsupported"
        assert "dispatch.codex_network_access" in row["warning"]


@pytest.mark.parametrize("network", [False, True])
def test_gitlab_writes_stay_rule_denied(droot: Path, network: bool) -> None:
    rows = _gitlab_preview(droot, network, GITLAB_WRITES)
    assert {r["verdict"] for r in rows.values()} == {"denied"}
    assert all("capability_verdict" not in r for r in rows.values())


def test_unknown_executable_stays_unverified_with_network(droot: Path) -> None:
    _set_alpha_dispatch(droot, codex_network_access=True)
    prompt = "Run.\n```sh\npython -c 'print(1)'\n```\n"
    preview = _new_fast(
        droot, provider="codex", model="gpt-6-luna", prompt=prompt, dry_run=True, allow_unmatched=True
    )
    row = next(r for r in preview["preflight"] if r["command"].startswith("python"))
    assert row["capability_verdict"] == "unverified"


def test_opaque_program_refuses_allocation_but_diagnostic_preserves_policy(
    droot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.dispatch import lifecycle
    from agent_cli.dispatch.state import load

    prompt = "Read a URL.\n```sh\npython -c '__import__(\"urllib.request\")'\n```\n"
    before = set((droot / ".agent" / "dispatches").glob("*.json"))
    with pytest.raises(AgentError, match="opaque executable effects"):
        _new_fast(droot, provider="codex", model="gpt-6-luna", prompt=prompt)
    assert set((droot / ".agent" / "dispatches").glob("*.json")) == before
    monkeypatch.setattr(lifecycle, "start", lambda root, d, **kwargs: d)
    attempted = _new_fast(droot, provider="codex", model="gpt-6-luna", prompt=prompt, allow_unmatched=True)
    preview = _new_fast(
        droot, provider="codex", model="gpt-6-luna", prompt=prompt, dry_run=True, allow_unmatched=True
    )
    assert (
        next(r for r in preview["preflight"] if r["command"].startswith("python"))["verdict"] == "unverified"
    )
    assert preview["options"]["sandbox_policy"]["networkAccess"] is False
    assert load(droot, attempted["id"])["provider"] == "codex"
