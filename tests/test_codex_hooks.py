"""Fail-closed hook discovery and process-scoped worker policy isolation."""

from __future__ import annotations

import asyncio
import copy
import re
import subprocess
import sys
import tomllib
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_cli import repos
from agent_cli.core import AgentError
from agent_cli.dispatch import codex_hooks, codex_policy, codex_transport, state
from agent_cli.dispatch.codex_permissions import hook_config
from dispatch_helpers import _new_fast, droot  # noqa: F401 -- pytest fixture


def hook(cwd: Path, **changes: Any) -> dict[str, Any]:
    return {
        "key": "project-key",
        "currentHash": "sha256:project",
        "source": "project",
        "sourcePath": str(cwd / ".codex/hooks.json"),
        "eventName": "sessionStart",
        "handlerType": "command",
        "command": "manager-capture",
        "enabled": True,
        "isManaged": False,
        "timeoutSec": 15,
        "trustStatus": "trusted",
        **changes,
    }


def policy(cwd: Path, **changes: Any) -> dict[str, Any]:
    expected = hook_config()["PreToolUse"][0]
    return hook(
        cwd,
        key="worker-key",
        currentHash="sha256:worker",
        source="sessionFlags",
        eventName="preToolUse",
        command=expected["hooks"][0]["command"],
        matcher=expected["matcher"],
        **changes,
    )


def listed(cwd: Path, hooks: list[dict[str, Any]]) -> dict[str, Any]:
    return {"data": [{"cwd": str(cwd), "errors": [], "warnings": [], "hooks": hooks}]}


def self_repo(path: Path) -> repos.Repo:
    return repos.Repo(name="Harness", short="harness", path=str(path), remote="")


def attempt(droot: Path, resume: bool) -> dict[str, Any]:
    inbox = droot / ".agent/scratch/absent-inbox"
    return {"text": "probe", "resume": resume, "mcp_servers": {}, "inbox": inbox, "timeout": 5}


def test_registry_relative_path_uses_harness_root_from_other_process_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A registry path '.' names the harness even when the supervisor runs elsewhere.
    root = tmp_path / "harness"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "init", "-q", str(elsewhere)], check=True)
    monkeypatch.chdir(elsewhere)
    repo = repos.Repo(name="Harness", short="harness", path=".", remote="")
    original = hook(root)
    assert codex_hooks.project_hooks(listed(root, [original]), root, repo, root) == [original]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda x: x.clear(),
        lambda x: x.update(data=[]),
        lambda x: x["data"].append(copy.deepcopy(x["data"][0])),
        lambda x: x["data"][0].pop("errors"),
        lambda x: x["data"][0].update(errors=[{"message": "parse error"}]),
        lambda x: x["data"][0].update(warnings=["unreadable source"]),
        lambda x: x["data"][0].update(cwd="/wrong"),
        lambda x: x["data"][0].update(hooks=None),
        lambda x: x["data"][0]["hooks"][0].pop("currentHash"),
        lambda x: x["data"][0]["hooks"][0].update(enabled=1),
        lambda x: x["data"][0]["hooks"][0].update(isManaged=None),
        lambda x: x["data"][0]["hooks"].append(copy.deepcopy(x["data"][0]["hooks"][0])),
    ],
)
def test_malformed_inventory_refuses(tmp_path: Path, mutation: Any) -> None:
    data = listed(tmp_path, [hook(tmp_path)])
    mutation(data)
    with pytest.raises(AgentError):
        codex_hooks._inventory(data, tmp_path)


def test_only_self_project_sources_can_be_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cwd = tmp_path / "worker"
    cwd.mkdir()
    monkeypatch.setattr(codex_hooks.worktrees, "main_checkout", lambda _path: tmp_path)
    repo = self_repo(tmp_path)
    hooks = [hook(tmp_path), hook(cwd, key="worktree-key")]
    assert codex_hooks.project_hooks(listed(cwd, hooks), tmp_path, repo, cwd) == hooks
    assert codex_hooks.worker_hooks(hooks)["state"] == {
        "project-key": {"enabled": False},
        "worktree-key": {"enabled": False},
    }
    monkeypatch.setattr(codex_hooks.worktrees, "main_checkout", lambda path: path)
    with pytest.raises(AgentError, match="foreign"):
        codex_hooks.project_hooks(listed(cwd, hooks), tmp_path, repo, cwd)


@pytest.mark.parametrize(
    "changes",
    [{"source": source} for source in ["user", "system", "plugin", "sessionFlags", "unknown"]]
    + [
        {"isManaged": True},
        {"sourcePath": "/foreign/.codex/hooks.json"},
        {"sourcePath": "relative/.codex/hooks.json"},
    ],
)
def test_foreign_hooks_refuse_even_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changes: dict[str, Any]
) -> None:
    monkeypatch.setattr(codex_hooks.worktrees, "main_checkout", lambda _path: tmp_path)
    with pytest.raises(AgentError, match="foreign"):
        codex_hooks.project_hooks(
            listed(tmp_path, [hook(tmp_path, enabled=False, **changes)]),
            tmp_path,
            self_repo(tmp_path),
            tmp_path,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": True},
        {"currentHash": "sha256:changed"},
        {"command": "different-manager"},
        {"eventName": "stop"},
        {"source": "user"},
        {"timeoutSec": 999},
    ],
)
def test_project_identity_or_disable_failure_refuses(tmp_path: Path, changes: dict[str, Any]) -> None:
    original = hook(tmp_path)
    changed = {**original, "enabled": False, **changes}
    with pytest.raises(AgentError):
        codex_hooks.verify_isolated(listed(tmp_path, [changed, policy(tmp_path)]), tmp_path, [original])


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": False},
        {"isManaged": True},
        {"source": "project"},
        {"command": "wrong-permission-hook"},
        {"matcher": "Bash"},
        {"timeoutSec": 99},
        {"async": True},
        {"eventName": "postToolUse"},
    ],
)
def test_wrong_generated_policy_refuses(tmp_path: Path, changes: dict[str, Any]) -> None:
    generated = {**policy(tmp_path), **changes}
    with pytest.raises(AgentError):
        codex_hooks.verify_isolated(listed(tmp_path, [generated]), tmp_path, [])


def test_missing_extra_or_removed_hooks_refuse(tmp_path: Path) -> None:
    original = hook(tmp_path)
    disabled = {**original, "enabled": False, "trustStatus": "untrusted"}
    generated = policy(tmp_path)
    codex_hooks.verify_isolated(listed(tmp_path, [disabled, generated]), tmp_path, [original])
    for hooks in [
        [],
        [disabled],
        [generated],
        [disabled, generated, hook(tmp_path, key="added")],
        [disabled, generated, {**generated, "key": "duplicate-policy"}],
    ]:
        with pytest.raises(AgentError):
            codex_hooks.verify_isolated(listed(tmp_path, hooks), tmp_path, [original])


def test_cli_config_round_trip_handles_paths_without_shell_interpretation(tmp_path: Path) -> None:
    project = hook(tmp_path, key='C:\\hooks\\"unicode-雪-$()')
    config = codex_hooks.worker_hooks([project])
    assert tomllib.loads("hooks=" + codex_hooks._toml(config))["hooks"] == config


def fake_servers(monkeypatch: pytest.MonkeyPatch, cwd: Path, reply: Any) -> list[Any]:
    """App-servers that answer only `hooks/list` for `cwd`, with `reply(instances)`."""
    instances: list[Any] = []

    class RPC:
        process = None

        def __init__(self, cwd: Path, **kwargs: Any):
            self.cwd, self.kwargs, self.closed = cwd, kwargs, False
            instances.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: Any):
            self.closed = True

        async def call(self, method: str, params: dict[str, Any]):
            assert method == "hooks/list" and params == {"cwds": [str(cwd)]}  # no thread starts
            return reply(instances)

    monkeypatch.setattr(codex_hooks, "CodexAppServer", RPC)
    # The fake RPC never runs the argv, so the test needs no codex on PATH (Linux CI has none).
    monkeypatch.setattr(codex_hooks, "command", lambda *args: ["codex", *args])
    monkeypatch.setattr(codex_policy, "command", lambda *args: ["codex", *args])
    return instances


async def enter(root: Path, repo: repos.Repo, cwd: Path, entered: list[Any]) -> None:
    env, deny = (
        {"AGENT_DISPATCH_ID": "001-mac-01"},
        lambda _r: asyncio.sleep(0, result={"decision": "decline"}),
    )
    async with codex_hooks.isolated_server(root, repo, cwd, env=env, handle_request=deny) as served:
        entered.append(served)


def test_fresh_process_rechecks_before_yield(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = hook(tmp_path)
    monkeypatch.setattr(codex_hooks.worktrees, "main_checkout", lambda _path: tmp_path)

    def reply(instances: list[Any]) -> dict[str, Any]:
        if len(instances) == 1:
            return listed(tmp_path, [original])
        assert instances[0].closed
        return listed(tmp_path, [{**original, "enabled": False}, policy(tmp_path)])

    instances, entered = fake_servers(monkeypatch, tmp_path, reply), []
    asyncio.run(enter(tmp_path, self_repo(tmp_path), tmp_path, entered))
    rpc, hooks = entered[0]
    assert rpc is instances[1]
    argv = instances[1].kwargs["command"]
    assert "-c" in argv
    assert tomllib.loads(argv[-1])["hooks"] == hooks
    assert hooks["state"] == {original["key"]: {"enabled": False}}
    assert instances[1].kwargs["env"] == instances[0].kwargs["env"]
    assert instances[1].kwargs["handle_request"] is instances[0].kwargs["handle_request"]
    assert all(instance.closed for instance in instances)


@pytest.mark.parametrize("resume", [False, True])
def test_initial_and_resume_keep_worker_hooks_and_permission_boundaries(
    droot: Path, monkeypatch: pytest.MonkeyPatch, resume: bool
) -> None:
    d = _new_fast(droot)
    repo = repos.load_registry(droot).require("alpha")
    d.update(codex_thread_id="existing-thread", model="gpt-6-luna")
    cwd = Path(d["worktree"])
    config = codex_hooks.worker_hooks([hook(cwd)])
    calls, approvals = [], []

    class RPC:
        process = None

        async def call(self, method: str, params: dict[str, Any]):
            calls.append((method, params))
            if method.startswith("thread/"):
                return {"thread": {"id": "existing-thread"}}
            # Stop before any model turn, after capturing the effective sandbox.
            raise AgentError("test stops before inference")

    @asynccontextmanager
    async def isolated(*_args: Any, **kwargs: Any):
        deny = kwargs["handle_request"]
        for method in (
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "item/tool/requestUserInput",
        ):
            approvals.append(await deny({"method": method, "params": {"command": "curl example.com"}}))
        yield RPC(), config

    monkeypatch.setattr(codex_transport, "isolated_server", isolated)
    result = codex_transport.run_codex(droot, d, repo, **attempt(droot, resume))
    assert result["session_established"]
    method, params = calls[0]
    assert method == ("thread/resume" if resume else "thread/start")
    assert params["config"]["hooks"] == config
    assert params["config"]["bypass_hook_trust"] is True
    assert params["config"]["agents"] == {"enabled": False}
    assert params["config"]["features"] == {"multi_agent": False}
    assert params["approvalPolicy"] == "on-request"
    assert params["sandbox"] == "workspace-write"
    sandbox = calls[1][1]["sandboxPolicy"]
    assert sandbox["type"] == "workspaceWrite"
    assert str(cwd) in sandbox["writableRoots"]
    assert sandbox["networkAccess"] is False
    assert calls[1][1]["approvalPolicy"] == "on-request"
    assert approvals == [{"decision": "decline"}, {"decision": "decline"}, {"answers": {}}]


HOOKS = b'{"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "true"}]}]}}\n'
EMPTY = b'{"hooks": {"PreToolUse": []}}\n'


def git(cwd: Path, *args: str) -> str:
    identity = ["-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]
    return subprocess.check_output(["git", *identity, "-C", str(cwd), *args], text=True).strip()


@pytest.fixture
def product(tmp_path: Path) -> SimpleNamespace:
    """A harness root, a distinct product clone tracking its own hooks, and a linked worker worktree."""
    root, clone, worker = tmp_path / "harness", tmp_path / "product", tmp_path / "worker"
    for path in (root, clone):
        subprocess.run(["git", "init", "-q", str(path)], check=True)
    (clone / ".codex").mkdir()
    (clone / ".codex/hooks.json").write_bytes(HOOKS)
    git(clone, "add", "-A")
    git(clone, "commit", "-qm", "hooks")
    git(clone, "worktree", "add", "-q", "-b", "agent/worker", str(worker))
    base = git(clone, "rev-parse", "HEAD")
    repo = repos.Repo(name="Product", short="product", path=str(clone), remote="")
    return SimpleNamespace(root=root, clone=clone, worker=worker, base=base, repo=repo)


def test_trusted_product_hooks_stay_enabled_beside_one_generated_policy(product: SimpleNamespace) -> None:
    hooks = [hook(product.clone), hook(product.worker, key="worktree-key")]
    data = listed(product.worker, hooks)
    assert codex_hooks.product_hooks(data, product.root, product.repo, product.worker) == hooks
    codex_hooks.pin_sources(hooks, product.worker, product.base)
    for base in ["origin/main", "0" * 40]:
        with pytest.raises(AgentError):
            codex_hooks.pin_sources(hooks, product.worker, base)
    assert codex_hooks.worker_hooks([])["state"] == {}
    generated = policy(product.worker)
    codex_hooks.verify_isolated(listed(product.worker, [*hooks, generated]), product.worker, [], hooks)
    first, second = hooks
    for after in [
        [{**first, "enabled": False}, second, generated],
        [{**first, "trustStatus": "untrusted"}, second, generated],
        [{**first, "currentHash": "sha256:changed"}, second, generated],
        [{**first, "command": "rewritten"}, second, generated],
        [second, generated],
        [*hooks],
        [*hooks, generated, {**generated, "key": "duplicate-policy"}],
        [*hooks, generated, hook(product.worker, key="added")],
    ]:
        with pytest.raises(AgentError):
            codex_hooks.verify_isolated(listed(product.worker, after), product.worker, [], hooks)


@pytest.mark.parametrize(
    "changes",
    [{"trustStatus": status} for status in ["untrusted", "modified", "managed"]]
    + [{"enabled": False}, {"enabled": False, "trustStatus": "untrusted"}, {"isManaged": True}]
    + [{"source": s, "enabled": False} for s in ["user", "system", "plugin", "sessionFlags", "unknown"]]
    + [
        {"sourcePath": "{tmp}/foreign/.codex/hooks.json"},
        {"sourcePath": ".codex/hooks.json"},
        {"sourcePath": "{tmp}/product/../product/.codex/hooks.json"},
        {"sourcePath": "{tmp}/product/.codex/config.toml"},
    ],
)
def test_product_hooks_refuse_unvetted_sources(product: SimpleNamespace, changes: dict[str, Any]) -> None:
    tmp = product.clone.parent
    changes = {k: v.format(tmp=tmp) if isinstance(v, str) else v for k, v in changes.items()}
    data = listed(product.worker, [hook(product.clone, **changes)])
    with pytest.raises(AgentError, match="refuses"):
        codex_hooks.product_hooks(data, product.root, product.repo, product.worker)


def test_product_hook_file_redirected_by_a_symlink_refuses(product: SimpleNamespace) -> None:
    (target := product.clone.parent / "elsewhere.json").write_bytes(HOOKS)
    source = product.worker / ".codex/hooks.json"
    source.unlink()
    try:
        source.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    data = listed(product.worker, [hook(product.worker)])
    with pytest.raises(AgentError, match="foreign"):
        codex_hooks.product_hooks(data, product.root, product.repo, product.worker)
    with pytest.raises(AgentError, match="missing or redirected"):
        codex_hooks.pin_sources([hook(product.worker)], product.worker, product.base)


def test_product_worker_must_be_a_git_worktree_of_the_registered_clone(product: SimpleNamespace) -> None:
    stranger = product.clone.parent / "stranger"
    subprocess.run(["git", "init", "-q", str(stranger)], check=True)
    wt_repo = repos.Repo(name="Product", short="product", path=str(product.worker), remote="")
    cases = [(product.repo, stranger), (wt_repo, product.worker), (product.repo, product.worker / ".codex")]
    for repo, cwd in cases:
        with pytest.raises(AgentError, match="registered clone"):
            codex_hooks.product_hooks(listed(cwd, []), product.root, repo, cwd)


def test_pinned_base_decides_what_a_native_selection_may_run(product: SimpleNamespace) -> None:
    # Native trust may cover a differing definition; the base still decides. With nothing selected,
    # only unchanged tracked files that define no hook pass.
    clone_file, other = product.clone / ".codex/hooks.json", b'{"hooks": {}}\n'
    trusted = [hook(product.clone, currentHash="sha256:newly-trusted")]
    git(product.clone, "checkout", "-qb", "other")
    clone_file.write_bytes(other)
    git(product.clone, "commit", "-qam", "the clone's other branch")
    for tracked, clone_bytes, selected, accepted in [
        (HOOKS, other, trusted, False),
        (HOOKS, HOOKS + b"\n", trusted, False),
        (HOOKS, None, [], False),
        (HOOKS, HOOKS, [], False),
        (b"{\n", b"{\n", [], False),
        (EMPTY, other, [], False),
        (EMPTY, None, [], False),
        (EMPTY, EMPTY, [], True),
        (b"{}\n", b"{}\n", [], True),
        (None, HOOKS, trusted, False),
        (None, None, [], True),
    ]:
        if tracked is None:
            git(product.worker, "rm", "-q", "--ignore-unmatch", ".codex/hooks.json")
        else:
            (product.worker / ".codex/hooks.json").write_bytes(tracked)
        git(product.worker, "commit", "--allow-empty", "-qam", "tracked hooks")
        base = git(product.worker, "rev-parse", "HEAD")
        clone_file.write_bytes(clone_bytes) if clone_bytes else clone_file.unlink(missing_ok=True)
        if accepted:
            codex_hooks.pin_sources(selected, product.worker, base)
            continue
        with pytest.raises(AgentError, match=re.escape(str(product.worker))) as refused:
            codex_hooks.pin_sources(selected, product.worker, base)
        assert base in str(refused.value) and (not selected or str(clone_file) in str(refused.value))


def test_a_crlf_checkout_of_the_pinned_blob_matches_only_on_windows(product: SimpleNamespace) -> None:
    hooks = [hook(product.clone), hook(product.worker, key="worktree-key")]
    worker_file = product.worker / ".codex/hooks.json"
    worker_file.write_bytes(HOOKS.replace(b"\n", b"\r\n"))
    if sys.platform == "win32":
        codex_hooks.pin_sources(hooks, product.worker, product.base)
    else:
        with pytest.raises(AgentError, match="differs"):
            codex_hooks.pin_sources(hooks, product.worker, product.base)
    worker_file.write_bytes((HOOKS + b"\n").replace(b"\n", b"\r\n"))
    with pytest.raises(AgentError, match="differs"):
        codex_hooks.pin_sources(hooks, product.worker, product.base)


@pytest.mark.parametrize("edit_after_compose", [False, True])
def test_product_server_pins_bytes_and_inventory_before_yield(
    product: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, edit_after_compose: bool
) -> None:
    state.save(product.root, {"id": "001-mac-01", "start_sha": product.base})
    original, entered = hook(product.clone), []

    def reply(instances: list[Any]) -> dict[str, Any]:
        if len(instances) == 1:
            return listed(product.worker, [original])
        if edit_after_compose:
            (product.clone / ".codex/hooks.json").write_bytes(HOOKS + b"\n")
        return listed(product.worker, [original, policy(product.worker)])

    instances = fake_servers(monkeypatch, product.worker, reply)
    run = enter(product.root, product.repo, product.worker, entered)
    if edit_after_compose:
        with pytest.raises(AgentError, match="differs"):
            asyncio.run(run)
        assert not entered and len(instances) == 2
    else:
        asyncio.run(run)
        assert entered[0][1]["state"] == {}
        assert tomllib.loads(instances[1].kwargs["command"][-1])["hooks"] == entered[0][1]


def test_product_server_needs_base(product: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    state.save(product.root, {"id": "001-mac-01"})
    instances, entered = fake_servers(monkeypatch, product.worker, lambda _i: listed(product.worker, [])), []
    with pytest.raises(AgentError, match="records no base commit"):
        asyncio.run(enter(product.root, product.repo, product.worker, entered))
    assert not instances and not entered


# Each copy once, initial and resume twice each: the full cross product is a dispatch per case.
@pytest.mark.parametrize(
    "resume, copy, added",
    [(False, "worker", HOOKS), (True, "worker", b""), (True, "clone", b""), (False, "clone", EMPTY)],
)
def test_unpinned_product_hook_refuses_before_thread_on_initial_and_resume(
    droot: Path, monkeypatch: pytest.MonkeyPatch, resume: bool, copy: str, added: bytes
) -> None:
    d = state.load(droot, _new_fast(droot, model="gpt-6-luna")["id"])
    d["codex_thread_id"], repo = "existing-thread", repos.load_registry(droot).require("alpha")
    cwd = Path(d["worktree"])
    path = (cwd if copy == "worker" else repo.abs_path(droot)) / ".codex/hooks.json"
    path.parent.mkdir()
    path.write_bytes(added)  # untracked: alpha's base has no hooks; one defining none selects none
    selected = [hook(path.parents[1])] if added == HOOKS else []
    instances = fake_servers(monkeypatch, cwd, lambda _i: listed(cwd, selected))
    result = codex_transport.run_codex(droot, d, repo, **attempt(droot, resume))
    assert len(instances) == 1
    assert d["start_sha"] and not result["session_established"] and result["exit_code"] == 1
    assert "tracks none" in state.file(droot, d["id"], ".err").read_text(encoding="utf-8")
