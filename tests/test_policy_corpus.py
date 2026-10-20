"""The checked-in policy corpus under `tests/policy_corpus/`, scored row by row.

It began as the deterministic half of the eval runner. The runner and its live and
manager probes were deleted in these rows stay as plain tests because they pin real
policy:

- `commands.yaml` `commands`: one Bash command each, fed through one named hook (`no-chaining`,
  `protect-generated` or `worker-redirect`), asserting `allow`/`deny`.
- `commands.yaml` `permission_rules`: bare commands scored through `permissions.matches` against
  BASE_ALLOW/BASE_DENY plus a named repo's rules, a forge's rules, or the manager's own
  `.claude/settings.json` (`rules: manager`), asserting `allowed`/`denied`/`no-rule`; a
  `decision` also checks the headless permission callback.
- `policy_outcomes.json`: `dispatch.evaluate()` cases, asserting `state` and `auto_resume`.
- `convention_gate.json`: worker diffs scored through `evaluate()` and its real diff gate.

Rows are scored against a scratch root holding the example registry, never the owner's.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from agent_cli import dispatch, forge, hooks, permissions, repos
from agent_cli.hooks.worker_redirect import outside_redirect
from agent_cli.records.dispatch import result_of
from agent_cli.records.narrow import as_dict, as_list, dig, str_list

ROOT = Path(__file__).resolve().parents[1]
CORPUS = Path(__file__).resolve().parent / "policy_corpus"
COMMANDS = yaml.safe_load((CORPUS / "commands.yaml").read_text(encoding="utf-8"))
POLICY = json.loads((CORPUS / "policy_outcomes.json").read_text(encoding="utf-8"))["cases"]
CONVENTIONS = json.loads((CORPUS / "convention_gate.json").read_text(encoding="utf-8"))["cases"]


@pytest.fixture(scope="module")
def root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The example registry and the manager's settings.json, which `rules: manager` rows read."""
    dest = tmp_path_factory.mktemp("corpus")
    (dest / "bin").mkdir()
    (dest / "bin" / "agent").write_text("")  # core.ROOT_MARKER
    shutil.copy(ROOT / "agent.example.yaml", dest / "agent.yaml")
    (dest / ".claude").mkdir()
    shutil.copy(ROOT / ".claude" / "settings.json", dest / ".claude" / "settings.json")
    return dest


def _fill(root: Path, cmd: str) -> str:
    """`<repo>` -> the first registered repo's clone path; `<root>` -> the scratch root."""
    first = repos.load_registry(root).repos[0].abs_path(root)
    return cmd.replace("<repo>", str(first)).replace("<root>", str(root))


def _worker_redirect(root: Path, payload: dict[str, Any]) -> str:
    reason = outside_redirect(
        str(dig(payload, "tool_input", "command") or ""), root / ".agent" / "eval-worker"
    )
    return "" if reason is None else json.dumps({"hookSpecificOutput": {"permissionDecision": "deny"}})


HOOKS = {
    "no-chaining": hooks.no_chaining,
    "protect-generated": hooks.protect_generated,
    "worker-redirect": _worker_redirect,
}


def _decision(out: str) -> str:
    if not out.strip():
        return "allow"
    return "deny" if json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny" else "allow"


def rule_lists(root: Path, row: dict[str, Any]) -> tuple[list[str], list[str]]:
    """The (allow, deny) a `permission_rules` row is scored against: a worker's by default
    (BASE_ALLOW plus the named repo's `allowed_tools`, against BASE_DENY), a forge's in place of
    the default forge's, or the manager's own `.claude/settings.json` for `rules: manager`. An
    unknown repo contributes no `allowed_tools`, so base-only rows still score on the example
    registry."""
    if row.get("rules") == "manager":
        block = json.loads((root / ".claude" / "settings.json").read_text(encoding="utf-8"))
        perms = as_dict(dig(block, "permissions"))
        return str_list(perms.get("allow")), str_list(perms.get("deny"))
    repo = repos.load_registry(root).find(row["repo"])
    allow = list(dispatch.BASE_ALLOW) + (repo.allowed_tools if repo is not None else [])
    deny = list(dispatch.BASE_DENY)
    if selected := row.get("forge"):
        default, chosen = forge.require(None), forge.require(str(selected))
        allow = [rule for rule in allow if rule not in default.allow] + list(chosen.allow)
        deny = [rule for rule in deny if rule not in default.deny] + list(chosen.deny)
    return allow, deny


def _id(row: dict[str, Any]) -> str:
    return str(row.get("name") or row.get("cmd"))[:60]


def test_the_corpus_has_its_minimum_rows():
    assert len(COMMANDS["commands"]) >= 20 and len(COMMANDS["permission_rules"]) >= 5
    assert len(POLICY) >= 6 and len(CONVENTIONS) >= 3


@pytest.mark.parametrize("row", COMMANDS["commands"], ids=_id)
def test_a_command_gets_its_hook_decision(root: Path, row: dict[str, Any]):
    out = HOOKS[row["hook"]](root, {"tool_name": "Bash", "tool_input": {"command": _fill(root, row["cmd"])}})
    assert _decision(out) == row["expect"], row.get("note")


@pytest.mark.parametrize("row", COMMANDS["permission_rules"], ids=_id)
def test_a_bare_command_gets_its_permission_verdict(root: Path, row: dict[str, Any]):
    allow, deny = rule_lists(root, row)
    cmd = _fill(root, row["cmd"])
    verdict, rule = permissions.matches(cmd, allow, deny)
    assert verdict == row["expect"], (rule, row.get("note"))
    if expected := row.get("decision"):
        repo = repos.Repo(name="Eval.Corpus", short="eval", path="repos/Eval.Corpus", remote="", dispatch={})
        assert dispatch.decide("Bash", {"command": cmd}, {}, repo, root)[0] == expected


def test_manager_rules_that_are_not_strings_are_dropped(tmp_path: Path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(
        json.dumps({"permissions": {"allow": [5, "Bash(ls)"], "deny": "x"}}), encoding="utf-8"
    )
    assert rule_lists(tmp_path, {"rules": "manager"}) == (["Bash(ls)"], [])


def test_every_gitlab_write_rule_has_a_corpus_command():
    rows = [
        row["cmd"]
        for row in COMMANDS["permission_rules"]
        if row.get("forge") == "gitlab" and row["expect"] == "denied"
    ]
    uncovered = [
        rule
        for rule in forge.GITLAB.deny
        if not any(permissions.matches(cmd, [], [rule])[0] == "denied" for cmd in rows)
    ]
    assert not uncovered


def test_a_gitlab_read_needs_the_gitlab_rules(root: Path):
    row = next(
        row
        for row in COMMANDS["permission_rules"]
        if row.get("forge") == "gitlab" and row["cmd"] == "glab mr view 1"
    )
    assert permissions.matches(row["cmd"], dispatch.BASE_ALLOW, dispatch.BASE_DENY)[0] == "no-rule"
    allow, deny = rule_lists(root, row)
    assert permissions.matches(row["cmd"], allow, deny)[0] == row["expect"]


def _policy_repo(case: dict[str, Any]) -> repos.Repo | None:
    """`evaluate()` reads required checks off the repo, so a case that names some gets one."""
    required = case.get("required_checks")
    if not required:
        return None
    checks = [str(c) for c in required]
    return repos.Repo(
        name="Eval.Corpus",
        short="eval",
        path="repos/Eval.Corpus",
        remote="",
        dispatch={"required_checks": checks},
    )


@pytest.mark.parametrize("case", POLICY, ids=_id)
def test_evaluate_matches_the_policy_table(tmp_path: Path, case: dict[str, Any]):
    """Each case's `worktree` does not exist, so `evaluate()`'s git cross-checks are no-ops."""
    outcome = dispatch.evaluate(tmp_path, dict(case["input"]), _policy_repo(case))
    expect = case["expect"]
    assert (outcome["state"], outcome.get("auto_resume")) == (expect["state"], expect["auto_resume"])


def _git(repo: Path, *args: str) -> str:
    argv = ["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args]
    return subprocess.run(argv, cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def _write_files(repo: Path, files: dict[str, str | None]) -> None:
    for rel, content in files.items():
        path = repo / rel
        if content is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")


@pytest.mark.parametrize("case", CONVENTIONS, ids=_id)
def test_the_convention_gate_scores_a_real_diff(tmp_path: Path, case: dict[str, Any]):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "eval@example.invalid")
    _git(tmp_path, "config", "user.name", "Eval")
    _write_files(tmp_path, case["base_files"])
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")
    _write_files(tmp_path, case["changed_files"])
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "worker change")
    result: dict[str, Any] = {
        "nonce": "eval-convention-nonce",
        "status": "done",
        "summary": "fixture change",
        "branch": "main",
        "commits": [],
        "files_touched": list(case["changed_files"]),
        "commands": [],
        "open_questions": [],
        "next_steps": [],
        "notes": "",
        "comments": case.get("comments", []),
        "conventions": case.get("conventions", []),
    }
    record: dict[str, object] = {
        "id": "eval-convention",
        "session_id": "eval-convention-session",
        "nonce": result["nonce"],
        "state": "running",
        "worktree": str(tmp_path),
        "base": base,
        "start_sha": base,
        "branch": "main",
        "exit_code": 0,
        "timed_out": False,
        "last_result": {
            "session_id": "eval-convention-session",
            "structured_output": result,
            "permission_denials": [],
        },
    }
    rp = repos.Repo(
        name="Eval.ConventionGate",
        short="eval",
        path="repos/Eval.ConventionGate",
        remote="",
        dispatch={"diff_rules": case.get("diff_rules", [])},
    )
    outcome = dispatch.evaluate(tmp_path, record, rp)
    expect = case["expect"]
    assert outcome["state"] == expect["state"]
    gate = as_dict(outcome.get("conventions"))
    for key, wanted in expect.items():
        if key not in ("state", "next_step_contains"):
            assert len(as_list(gate.get(key))) == wanted, key
    if needle := expect.get("next_step_contains"):
        assert any(needle in step for step in result_of(outcome).get("next_steps") or [])
