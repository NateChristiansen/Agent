"""Inventory a Codex worker's hooks: isolate this harness's manager hooks, keep a product repo's
natively trusted hooks pinned to the dispatch base, and add one generated worker policy."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from .. import repos
from .. import worktree as worktrees
from ..codex_executable import command
from ..core import AgentError, ExternalFailure
from ..records.narrow import as_dict_or_none, as_int, as_list_or_none, as_str
from . import state
from .codex_permissions import hook_config
from .codex_policy import CodexPolicy
from .codex_rpc import CodexAppServer, RequestHandler

HOOKS_FILE = ".codex/hooks.json"
BASE_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _inventory(listed: dict[str, Any], cwd: Path) -> list[dict[str, Any]]:
    entries = as_list_or_none(listed.get("data"))
    if entries is None or len(entries) != 1:
        raise AgentError("Codex hook inventory is unavailable")
    entry = as_dict_or_none(entries[0])
    listing = as_list_or_none(entry.get("hooks")) if entry is not None else None
    if (
        entry is None
        or entry.get("cwd") != str(cwd)
        or entry.get("errors") != []
        or entry.get("warnings") != []
        or listing is None
    ):
        raise AgentError("Codex hook inventory is malformed or has diagnostics")
    seen: set[str] = set()
    hooks: list[dict[str, Any]] = []
    for raw in listing:
        hook = as_dict_or_none(raw)
        if (
            hook is None
            or any(
                not isinstance(hook.get(key), str) or not hook[key]
                for key in ("key", "currentHash", "source", "sourcePath", "eventName", "handlerType")
            )
            or type(hook.get("enabled")) is not bool
            or type(hook.get("isManaged")) is not bool
            or as_int(hook.get("timeoutSec"), -1) < 0
            or hook.get("trustStatus") not in {"managed", "untrusted", "trusted", "modified"}
            or hook["handlerType"] not in {"command", "mcpTool", "prompt", "agent"}
            or (hook["handlerType"] == "command" and not isinstance(hook.get("command"), str))
            or (hook.get("matcher") is not None and not isinstance(hook["matcher"], str))
            or hook["key"] in seen
        ):
            raise AgentError("Codex hook inventory contains malformed or duplicate hooks")
        seen.add(as_str(hook["key"]))
        hooks.append(hook)
    return hooks


def _harness_checkout(root: Path, repo: repos.Repo, cwd: Path) -> Path | None:
    """This harness's main checkout when the dispatch runs in it, else None."""
    try:
        main = worktrees.main_checkout(root).resolve()
        if (
            worktrees.main_checkout(repo.abs_path(root)).resolve() == main
            and worktrees.main_checkout(cwd).resolve() == main
        ):
            return main
    except AgentError, ExternalFailure, OSError:
        pass
    return None


def _project_sources(hooks: list[dict[str, Any]], allowed: set[Path]) -> None:
    for hook in hooks:
        source = Path(hook["sourcePath"])
        if (
            hook["source"] != "project"
            or hook["isManaged"]
            or not source.is_absolute()
            or source not in allowed
            or source.resolve() != source
        ):
            raise AgentError("Codex dispatch refuses foreign or managed hooks")


def project_hooks(listed: dict[str, Any], root: Path, repo: repos.Repo, cwd: Path) -> list[dict[str, Any]]:
    """Only this harness's project hook files are eligible for process-local disabling."""
    hooks = _inventory(listed, cwd)
    main = _harness_checkout(root, repo, cwd)
    _project_sources(hooks, {main / HOOKS_FILE, cwd / HOOKS_FILE} if main else set())
    return hooks


def _product_checkout(root: Path, repo: repos.Repo, cwd: Path) -> Path:
    """The registered product clone, once `cwd` is checked to be it or one of its Git worktrees."""
    clone = repo.abs_path(root)
    try:
        listed = {Path(w["path"]).resolve() for w in repos.worktrees(clone) if w.get("path")}
        if (
            worktrees.main_checkout(clone).resolve() != clone
            or worktrees.main_checkout(cwd).resolve() != clone
            or cwd.resolve() not in listed
        ):
            raise AgentError(f"worker {cwd} is not a Git worktree of the registered clone {clone}")
    except (ExternalFailure, OSError) as exc:
        raise AgentError(f"cannot check worker {cwd} against the registered clone {clone}: {exc}") from None
    return clone


def product_hooks(listed: dict[str, Any], root: Path, repo: repos.Repo, cwd: Path) -> list[dict[str, Any]]:
    """A product repo's own hooks stay active only as the native client already trusts them."""
    hooks = _inventory(listed, cwd)
    clone = _product_checkout(root, repo, cwd)
    _project_sources(hooks, {clone / HOOKS_FILE, cwd / HOOKS_FILE})
    for hook in hooks:
        if hook["trustStatus"] != "trusted" or not hook["enabled"]:
            raise AgentError(
                f"Codex dispatch refuses product hook {hook['key']} from {hook['sourcePath']}: the "
                f"worker's native inventory reports it {hook['trustStatus']}"
                f"{'' if hook['enabled'] else ' and disabled'}. Review, trust and enable the current "
                "definition in Codex first; the dispatch never promotes it."
            )
    return hooks


def _git_bytes(cwd: Path, *args: str) -> bytes:
    try:
        proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AgentError(f"cannot read Git in {cwd}: {exc}") from None
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise AgentError(f"git {' '.join(args)} failed in {cwd}: {detail}")
    return proc.stdout


def base_hooks(cwd: Path, base: str) -> bytes | None:
    if not BASE_RE.fullmatch(base):
        raise AgentError(f"product hook base {base!r} is not a commit id")
    entry = _git_bytes(cwd, "ls-tree", "-z", "--full-tree", f"{base}^{{commit}}", "--", HOOKS_FILE)
    if not entry:
        return None
    meta, _, _path = entry.rstrip(b"\0").partition(b"\t")
    fields = meta.split()
    if len(fields) != 3 or fields[0] not in (b"100644", b"100755") or fields[1] != b"blob":
        raise AgentError(f"{HOOKS_FILE} at base {base} is not a regular file")
    return _git_bytes(cwd, "cat-file", "blob", fields[2].decode("ascii"))


def _defines_no_hooks(data: bytes) -> bool:
    """True only for a well-formed hook file whose event lists hold no handler."""
    try:
        config = as_dict_or_none(json.loads(data.decode("utf-8")))
    except ValueError:
        return False
    if config is None or set(config) - {"hooks"}:
        return False
    events = as_dict_or_none(config.get("hooks", {}))
    if events is None:
        return False
    for groups in events.values():
        listed = as_list_or_none(groups)
        if listed is None:
            return False
        for group in listed:
            entry = as_dict_or_none(group)
            handlers = as_list_or_none(entry.get("hooks", [])) if entry is not None else None
            if handlers is None or handlers:
                return False
    return True


def _crlf_checkout(blob: bytes) -> bytes | None:
    """The bytes Git for Windows' default core.autocrlf=true checks an LF blob out as."""
    if os.name != "nt" or b"\r" in blob:
        return None
    return blob.replace(b"\n", b"\r\n")


def pin_sources(hooks: list[dict[str, Any]], cwd: Path, base: str) -> None:
    """Native trust covers a definition hash, not the commit it came from: the selected hook file
    can be the registered clone's working tree, dirty or on another branch."""
    pinned = base_hooks(cwd, base)
    sources = sorted({str(h["sourcePath"]) for h in hooks})
    if not sources:
        if pinned is not None and not _defines_no_hooks(pinned):
            raise AgentError(
                f"Codex dispatch refuses product hooks: base {base} tracks {HOOKS_FILE}, but the native "
                f"inventory for worker {cwd} selects no hook source"
            )
        # Nothing was selected, so every file the client may select must still hold the pinned
        # bytes, or still be absent when the base tracks none.
        sources = [str(worktrees.main_checkout(cwd).resolve() / HOOKS_FILE), str(cwd / HOOKS_FILE)]
    for source in sources:
        path = Path(source)
        try:
            current = path.read_bytes() if not path.is_symlink() else None
        except OSError:
            current = None
        if pinned is None:
            if hooks or os.path.lexists(path):
                problem = f"{HOOKS_FILE} at base {base} tracks none"
            else:
                continue
        elif current is None:
            problem = f"it is missing or redirected, but base {base} tracks {HOOKS_FILE}"
        elif current != pinned and current != _crlf_checkout(pinned):
            problem = f"{HOOKS_FILE} at base {base} differs"
        else:
            continue
        raise AgentError(f"Codex dispatch refuses product hook source {source} for worker {cwd}: {problem}")


def _pinned_base(root: Path, env: dict[str, str]) -> str:
    """The commit this dispatch began from, as its record keeps it across resumes."""
    dispatch_id = env.get("AGENT_DISPATCH_ID", "")
    try:
        base = str(state.load(root, dispatch_id).get("start_sha") or "")
    except AgentError:
        base = ""
    if not BASE_RE.fullmatch(base):
        raise AgentError(
            f"Codex dispatch {dispatch_id or '(unnamed)'} records no base commit to pin product hooks against"
        )
    return base


def worker_hooks(project: list[dict[str, Any]]) -> dict[str, Any]:
    return {**hook_config(), "state": {h["key"]: {"enabled": False} for h in project}}


def _identity(hook: dict[str, Any]) -> dict[str, Any]:
    # Disabling changes presentation/trust status, but never the handler's identity.
    return {
        key: value
        for key, value in hook.items()
        if key not in {"enabled", "trustStatus", "statusMessage", "displayOrder"}
    }


def verify_isolated(
    listed: dict[str, Any],
    cwd: Path,
    project: Sequence[dict[str, Any]],
    product: Sequence[dict[str, Any]] = (),
) -> None:
    """`project` hooks must now be disabled and `product` hooks still enabled and trusted, each
    with its identity unchanged, beside exactly one generated policy."""
    hooks = _inventory(listed, cwd)
    disabled = {h["key"]: _identity(h) for h in project}
    active = {h["key"]: _identity(h) for h in product}
    expected = {**disabled, **active}
    policy = hook_config()["PreToolUse"][0]
    generated: list[dict[str, Any]] = []
    observed: dict[str, dict[str, Any]] = {}
    for hook in hooks:
        if hook["key"] in disabled:
            if hook["enabled"] or _identity(hook) != disabled[hook["key"]]:
                raise AgentError("Codex project hook isolation changed or failed")
            observed[hook["key"]] = _identity(hook)
        elif hook["key"] in active:
            if (
                not hook["enabled"]
                or hook["trustStatus"] != "trusted"
                or _identity(hook) != active[hook["key"]]
            ):
                raise AgentError(
                    f"Codex product hook {hook['key']} changed, lost trust or was disabled with the worker policy"
                )
            observed[hook["key"]] = _identity(hook)
        else:
            if (
                hook["source"] != "sessionFlags"
                or hook["isManaged"]
                or not hook["enabled"]
                or hook["eventName"] != "preToolUse"
                or hook["handlerType"] != "command"
                or hook.get("command") != policy["hooks"][0]["command"]
                or hook.get("matcher") != policy["matcher"]
                or hook.get("timeoutSec") != 15
                or hook.get("async", False) is not False
            ):
                raise AgentError("Codex dispatch refuses unexpected hooks after isolation")
            generated.append(hook)
    if observed != expected or len(generated) != 1:
        raise AgentError("Codex worker hook is missing or project inventory changed")


def _toml(value: object) -> str:
    """Encode our small generated config as one CLI TOML inline value."""
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    if (items := as_list_or_none(value)) is not None:
        return "[" + ",".join(_toml(v) for v in items) + "]"
    if (table := as_dict_or_none(value)) is not None:
        return "{" + ",".join(_toml(k) + "=" + _toml(v) for k, v in table.items()) + "}"
    raise AgentError("unsupported generated Codex hook config value")


@asynccontextmanager
async def isolated_server(
    root: Path,
    repo: repos.Repo,
    cwd: Path,
    *,
    env: dict[str, str],
    handle_request: RequestHandler,
    policy: CodexPolicy | None = None,
) -> AsyncGenerator[tuple[CodexAppServer, dict[str, Any]]]:
    base = None if _harness_checkout(root, repo, cwd) else _pinned_base(root, env)
    # Discovery never starts a thread and therefore never runs manager session hooks.
    async with CodexAppServer(
        cwd, env=env, handle_request=handle_request, command=policy.server_command() if policy else None
    ) as discovery:
        listed = await discovery.call("hooks/list", {"cwds": [str(cwd)]})
    project: list[dict[str, Any]] = []
    product: list[dict[str, Any]] = []
    if base is None:
        project = project_hooks(listed, root, repo, cwd)
    else:
        product = product_hooks(listed, root, repo, cwd)
        pin_sources(product, cwd, base)
    config = worker_hooks(project)
    overrides = ("-c", "hooks=" + _toml(config))
    argv = (
        policy.server_command(*overrides) if policy else tuple(command("app-server", "--stdio", *overrides))
    )
    async with CodexAppServer(cwd, env=env, command=argv, handle_request=handle_request) as rpc:
        verify_isolated(await rpc.call("hooks/list", {"cwds": [str(cwd)]}), cwd, project, product)
        if base is not None:
            # The thread's trust bypass must meet exactly the bytes checked above.
            pin_sources(product, cwd, base)
        yield rpc, config
