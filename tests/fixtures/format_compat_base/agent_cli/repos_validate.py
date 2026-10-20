# ruff: noqa
from __future__ import annotations
import math
from collections.abc import Mapping
from difflib import get_close_matches
from . import forge as forge_mod
from .core import AgentError
from .launch_flags import parse_extra_args
from .records.narrow import as_dict_or_none, as_list_or_none
from .repos import (
    BRANCH_TEMPLATE_FIELDS,
    DEFAULT_TIMEOUT_MINUTES,
    EFFORT_LEVELS,
    PERMISSION_MODES,
    Repo,
    diff_rules,
    verification_config,
)

SHARED_KEYS = frozenset({"version", "forge", "owner", "worker_mcp", "repos", "peers", "journal", "hooks"})
REPO_KEYS = frozenset(
    {
        "name",
        "short",
        "path",
        "remote",
        "default_branch",
        "worktrees_dir",
        "forge",
        "harness",
        "notes",
        "dispatch",
    }
)
DISPATCH_KEYS = frozenset(
    {
        "model",
        "effort",
        "permission_mode",
        "max_budget_usd",
        "timeout_minutes",
        "max_turns",
        "max_tool_items",
        "extra_args",
        "allowed_tools",
        "verify",
        "verify_timeout_seconds",
        "max_concurrent",
        "max_concurrent_verify",
        "max_pending_verify",
        "verify_wait_timeout_s",
        "setup",
        "probes",
        "env",
        "env_allow",
        "branch_template",
        "repo_mcp",
        "diff_rules",
        "codex_network_access",
        "model_from",
        "required_checks",
    }
)


def _unknown_keys(data: Mapping[str, object], known: frozenset[str], path: str) -> list[str]:
    problems: list[str] = []
    for key in sorted(data, key=str):
        if key in known:
            continue
        matches = get_close_matches(str(key), sorted(known), n=1)
        hint = f"; did you mean {matches[0]!r}?" if matches else ""
        problems.append(f"{path}: unknown key {key!r}{hint}")
    return problems


def registry_unknown_keys(data: object) -> list[str]:
    shared = as_dict_or_none(data)
    if shared is None:
        return []
    problems = _unknown_keys(shared, SHARED_KEYS, "agent.yaml")
    for index, raw in enumerate(as_list_or_none(shared.get("repos")) or []):
        repo = as_dict_or_none(raw)
        if repo is None:
            continue
        path = f"repos[{index}]"
        problems.extend(_unknown_keys(repo, REPO_KEYS, path))
        dispatch = as_dict_or_none(repo.get("dispatch"))
        if dispatch is not None:
            problems.extend(_unknown_keys(dispatch, DISPATCH_KEYS, f"{path}.dispatch"))
    return problems


def positive_finite_minutes(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        minutes = float(value)
    except OverflowError:
        return None
    return minutes if math.isfinite(minutes) and minutes > 0 else None


def check_timeout_minutes(value: object) -> float:
    minutes = positive_finite_minutes(value)
    if minutes is None:
        raise AgentError(f"timeout_minutes must be a positive, finite number of minutes; got {value!r}")
    return minutes


def resolve_timeout(repo: Repo, passed: object | None) -> tuple[float, str]:
    if passed is not None:
        return (check_timeout_minutes(passed), "passed")
    raw = repo.dispatch.get("timeout_minutes")
    if raw is None:
        return (float(DEFAULT_TIMEOUT_MINUTES), "default")
    minutes = positive_finite_minutes(raw)
    if minutes is None:
        raise AgentError(
            f"{repo.short}: dispatch.timeout_minutes must be a positive, finite number of minutes; got {raw!r}. Fix the repo setting or pass timeout_minutes"
        )
    return (minutes, "repo")


def _is_label(value: object) -> bool:
    from .dispatch.ids import TAG_RE

    return isinstance(value, str) and TAG_RE.match(value) is not None


def validate_registry_data(data: object, *, unknown_keys: bool = True) -> list[str]:
    problems = registry_unknown_keys(data) if unknown_keys else []
    data = as_dict_or_none(data)
    if data is None:
        return ["top level must be a mapping"]
    repos = as_list_or_none(data.get("repos"))
    if repos is None:
        return [*problems, "'repos' must be a list"]
    from . import worker_mcp
    from .hooks import toggles

    problems.extend(worker_mcp.validate(data.get(worker_mcp.BLOCK)))
    problems.extend(toggles.problems(data.get("hooks"), "agent.yaml"))
    given_owner = data.get("owner")
    owner = as_dict_or_none(given_owner)
    if given_owner is not None and (owner is None or not str(owner.get("name") or "").strip()):
        problems.append("'owner' must be a mapping with a non-empty name")
    peers = data.get("peers")
    peers_map = as_dict_or_none(peers)
    seconds = peers_map.get("defer_nonurgent_s", 0) if peers_map is not None else None
    if peers is not None and (
        peers_map is None
        or set(peers_map) - {"defer_nonurgent_s"}
        or (not isinstance(seconds, int))
        or isinstance(seconds, bool)
        or (seconds < 0)
    ):
        problems.append("'peers' must be a mapping; peers.defer_nonurgent_s is whole seconds, 0 or more")
    journal = data.get("journal")
    journal_map = as_dict_or_none(journal)
    if journal is not None and (
        journal_map is None
        or set(journal_map) - {"machine"}
        or (not _is_label(journal_map.get("machine", "a")))
    ):
        problems.append(
            "'journal' must be a mapping; journal.machine is a machine label (1-8 lowercase letters and digits, starting with a letter)"
        )
    registry_forge = data.get("forge")
    if registry_forge is not None and (
        not isinstance(registry_forge, str) or str(registry_forge).strip().lower() not in forge_mod.FORGES
    ):
        problems.append(f"'forge' must be one of {', '.join(sorted(forge_mod.FORGES))}")
    seen: set[str] = set()
    for i, raw in enumerate(repos):
        r = as_dict_or_none(raw)
        if r is None:
            problems.append(f"repos[{i}] must be a mapping")
            continue
        for key in ("name", "path"):
            if not r.get(key):
                problems.append(f"repos[{i}] missing {key}")
        mine = {str(r[key]).lower() for key in ("name", "short") if r.get(key)}
        for val in sorted(mine):
            if val in seen:
                problems.append(f"duplicate repo name/short {val!r}")
        seen |= mine
        from .config_file import not_relative

        for key in ("path", "worktrees_dir"):
            value = r.get(key)
            if value is not None and (not isinstance(value, str)):
                problems.append(f"repos[{i}] {key} must be a path")
            elif isinstance(value, str) and not_relative(value):
                problems.append(
                    f"repos[{i}] {key} must be relative to the harness root; put a machine's absolute or ~ path in agent.local.yaml"
                )
        repo_forge = r.get("forge")
        if repo_forge is not None and (
            not isinstance(repo_forge, str) or repo_forge.strip().lower() not in forge_mod.FORGES
        ):
            problems.append(f"repos[{i}] forge must be one of {', '.join(sorted(forge_mod.FORGES))}")
        dispatch = r.get("dispatch")
        dispatch_map = as_dict_or_none(dispatch)
        if dispatch is not None and dispatch_map is None:
            problems.append(f"repos[{i}] dispatch must be a mapping")
        elif dispatch_map:
            problems.extend((f"repos[{i}] {p}" for p in validate_dispatch(dispatch_map, unknown_keys=False)))
    return problems


def _branch_template_problems(template: str) -> list[str]:
    problems: list[str] = []
    try:
        rendered = template.format(**{f: "x" for f in BRANCH_TEMPLATE_FIELDS})
    except (IndexError, KeyError) as exc:
        return [
            f"names {exc}, which a dispatch cannot supply; use only {', '.join(('{' + f + '}' for f in BRANCH_TEMPLATE_FIELDS))}"
        ]
    if any((c.isspace() for c in rendered)):
        problems.append("must not contain whitespace")
    if not any((f"{{{f}}}" in template for f in ("slug", "number", "ticket"))):
        problems.append(
            "must name at least one of {slug}, {number} or {ticket}, or every feature would share one branch"
        )
    if rendered.startswith("/") or rendered.endswith("/") or ".." in rendered:
        problems.append("is not a valid git branch name")
    return problems


def branch_suffix_problem(template: str) -> str | None:
    import string

    try:
        fields = [(name, spec, conv) for _, name, spec, conv in string.Formatter().parse(template)]
    except ValueError:
        fields = []
    if ("suffix", "", None) in fields:
        return None
    return f"branch_template {template!r} must name a plain {{suffix}} field (not escaped, no conversion or format spec), which renders -<tag>-SS so each machine's dispatches get their own branch (ADR-0036)"


def validate_dispatch(dispatch: Mapping[str, object], *, unknown_keys: bool = True) -> list[str]:
    problems = _unknown_keys(dispatch, DISPATCH_KEYS, "dispatch") if unknown_keys else []
    from .repos import dispatch_bool

    try:
        dispatch_bool(dispatch, "codex_network_access")
    except AgentError as exc:
        problems.append(exc.message)
    try:
        verification_config(dispatch)
    except ValueError as exc:
        problems.append(str(exc))
    try:
        diff_rules(dispatch)
    except ValueError as exc:
        problems.append(str(exc))
    tools = dispatch.get("allowed_tools")
    tool_list = as_list_or_none(tools)
    if tools is not None and (
        tool_list is None or not all((isinstance(t, str) and t.strip() for t in tool_list))
    ):
        problems.append(
            "dispatch.allowed_tools must be a list of permission strings (e.g. 'Bash(uv run pytest:*)')"
        )
    model = dispatch.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        problems.append("dispatch.model must be null or a non-empty string")
    if dispatch.get("model_from") is not None:
        problems.append(
            "dispatch.model_from is no longer read: remove it, and pin a model with dispatch.model if the repo needs one"
        )
    effort = dispatch.get("effort")
    if effort is not None and effort not in EFFORT_LEVELS:
        problems.append(f"dispatch.effort must be one of {', '.join(EFFORT_LEVELS)}")
    timeout = dispatch.get("timeout_minutes")
    if timeout is not None and positive_finite_minutes(timeout) is None:
        problems.append("dispatch.timeout_minutes must be a positive, finite number")
    for key in (
        "max_turns",
        "max_tool_items",
        "max_concurrent",
        "max_concurrent_verify",
        "max_pending_verify",
    ):
        value = dispatch.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
            problems.append(f"dispatch.{key} must be a positive integer")
    wait = dispatch.get("verify_wait_timeout_s")
    if wait is not None and (isinstance(wait, bool) or not isinstance(wait, (int, float)) or wait <= 0):
        problems.append("dispatch.verify_wait_timeout_s must be a positive number of seconds")
    budget = dispatch.get("max_budget_usd")
    if budget is not None and (
        isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget <= 0
    ):
        problems.append("dispatch.max_budget_usd must be a positive number")
    setup = dispatch.get("setup")
    setup_list = as_list_or_none(setup)
    if setup is not None and (
        setup_list is None or not all((isinstance(c, str) and c.strip() for c in setup_list))
    ):
        problems.append("dispatch.setup must be a list of shell commands run in a fresh worktree")
    probes = dispatch.get("probes")
    probe_list = as_list_or_none(probes)
    if probes is not None and (
        probe_list is None or not all((isinstance(c, str) and c.strip() for c in probe_list))
    ):
        problems.append("dispatch.probes must be a list of non-empty shell command strings")
    if "required_checks" in dispatch:
        problems.append(
            "dispatch.required_checks is retired: workers no longer report commands, so nothing can match it. Remove the key and list each check as an executable dispatch.verify entry (command plus timeout_seconds, with dispatch.verify_timeout_seconds); the supervisor runs those itself"
        )
    mode = dispatch.get("permission_mode")
    if mode is not None and mode not in PERMISSION_MODES:
        problems.append(f"dispatch.permission_mode must be one of {', '.join(PERMISSION_MODES)}")
    try:
        parse_extra_args(dispatch.get("extra_args"))
    except ValueError as exc:
        problems.append(str(exc))
    env = dispatch.get("env")
    env_map = as_dict_or_none(env)
    pairs: list[tuple[object, object]] = list(env_map.items()) if env_map is not None else []
    if env is not None and (
        env_map is None
        or not all(
            (
                isinstance(k, str)
                and k.strip()
                and isinstance(v, (str, int, float))
                and (not isinstance(v, bool))
                for k, v in pairs
            )
        )
    ):
        problems.append("dispatch.env must be a mapping of NAME to a string or number")
    template = dispatch.get("branch_template")
    if template is not None:
        if not isinstance(template, str) or not template.strip():
            problems.append(
                f"dispatch.branch_template must be a non-empty format string over {', '.join(('{' + f + '}' for f in BRANCH_TEMPLATE_FIELDS))}"
            )
        else:
            problems.extend((f"dispatch.branch_template {p}" for p in _branch_template_problems(template)))
    repo_mcp = dispatch.get("repo_mcp")
    mcp_list = as_list_or_none(repo_mcp)
    if repo_mcp is not None and (
        mcp_list is None or not all((isinstance(s, str) and s.strip() for s in mcp_list))
    ):
        problems.append("dispatch.repo_mcp must be a list of server names from the repo's own .mcp.json")
    elif mcp_list is not None and len(mcp_list) != len(set(mcp_list)):
        problems.append("dispatch.repo_mcp must not repeat server names")
    env_allow = dispatch.get("env_allow")
    allow_list = as_list_or_none(env_allow)
    if env_allow is not None and (
        allow_list is None
        or not all((isinstance(n, str) and n.strip() and ("=" not in n) for n in allow_list))
    ):
        problems.append(
            "dispatch.env_allow must be a list of environment variable names, or NAME_* prefixes, a worker may inherit (no values: use dispatch.env to set one)"
        )
    return problems
