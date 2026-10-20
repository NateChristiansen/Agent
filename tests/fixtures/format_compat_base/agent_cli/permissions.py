# ruff: noqa
from __future__ import annotations
import fnmatch
import re
from collections.abc import Iterable
from .quote_mask import mask_quotes

RULE_RE = re.compile("^(?P<tool>[A-Za-z_][A-Za-z0-9_-]*(?:\\*)?)(?:\\((?P<arg>.*)\\))?$")
MID_TOKEN_END = ("/", "-", "=", ".", "_", ",", ":")


def _bash_prefix(arg: str) -> str | None:
    return arg[:-2] if arg.endswith(":*") else None


def _stops_mid_token(prefix: str) -> bool:
    if "*" in prefix:
        return False
    if prefix.rsplit(" ", 1)[-1] == "--":
        return False
    return prefix.endswith(MID_TOKEN_END)


REDACTED_RULE = "[redacted-rule]"
REDACTED_MCP_RULE = "[redacted-mcp-rule]"
REDACTED_TOOL = "[redacted-tool]"
REDACTED_MCP_TOOL = "[redacted-mcp-tool]"
REDACTED_ENDPOINT = "[redacted-endpoint]"
_ENDPOINT_RE = re.compile("[A-Za-z][A-Za-z0-9+.-]*://")
_ENDPOINT_RUN_RE = re.compile(_ENDPOINT_RE.pattern + "\\S*")


def _label(name: str) -> str:
    from .dispatch.mcp_policy import _label

    return _label(name)


def _secrets(hide: Iterable[str]) -> list[str]:
    return [name for name in hide if name and _label(name) != name]


def shown_rule(rule: str, hide: Iterable[str] = ()) -> str:
    body = rule.strip()
    if body.startswith("mcp__"):
        parsed = RULE_RE.fullmatch(body)
        if parsed is None or parsed.group("arg") is not None:
            return REDACTED_MCP_RULE
    if _ENDPOINT_RE.search(rule) or any((name in rule for name in _secrets(hide))):
        return REDACTED_RULE
    return repr(rule)


def shown_tool(tool: str) -> str:
    if _label(tool) == tool:
        return tool
    return REDACTED_MCP_TOOL if tool.startswith("mcp__") else REDACTED_TOOL


def shown_text(text: str) -> str:
    return _ENDPOINT_RUN_RE.sub(REDACTED_ENDPOINT, text)


def _fix(rule: str, shown: str, suggestion: str, withheld: str, hide: list[str]) -> str:
    if shown != repr(rule) or shown_rule(suggestion, hide) != repr(suggestion):
        return withheld
    return f"`{suggestion}`"


def lint(rules: list[str], *, label: str, hide: Iterable[str] = ()) -> list[str]:
    hide = list(hide)
    problems: list[str] = []
    seen: dict[str, int] = {}
    for rule in rules:
        seen[rule] = seen.get(rule, 0) + 1
        shown = shown_rule(rule, hide)
        match = RULE_RE.match(rule.strip())
        if not match:
            problems.append(
                f"{label}: {shown} is not a permission rule (expected Tool, Tool(arg) or Tool(prefix:*))"
            )
            continue
        arg = match.group("arg")
        if match.group("tool") != "Bash" or arg is None:
            continue
        prefix = _bash_prefix(arg)
        if prefix is None:
            continue
        if not prefix.strip():
            problems.append(f"{label}: {shown} matches every Bash command; say what you mean")
            continue
        if "*" in prefix:
            fix = _fix(rule, shown, f"Bash({prefix}*)", "the prefix with one trailing `*` and no `:`", hide)
            problems.append(
                f"{label}: {shown} mixes `*` with the trailing `:*`, so Claude Code reads the whole rule as a literal prefix and it matches nothing. Write {fix}."
            )
            continue
        if _stops_mid_token(prefix):
            whole = f"Bash({prefix.rstrip(''.join(MID_TOKEN_END))}:*)"
            fix = _fix(rule, shown, whole, "a prefix that ends on a whole token", hide)
            problems.append(
                f"{label}: {shown} stops mid-token, so it matches nothing. A prefix rule has to end on a whole token: write {fix} or spell the command out."
            )
    problems += [
        f"{label}: {shown_rule(rule, hide)} listed {count} times" for rule, count in seen.items() if count > 1
    ]
    return problems


def shadowed(
    allow: list[str],
    deny: list[str],
    hide: Iterable[str] = (),
    *,
    allow_label: str = "allow",
    deny_label: str = "deny",
) -> list[str]:
    hide = list(hide)
    denied = set(deny)
    return [
        f"{allow_label} {shown_rule(rule, hide)} is cancelled by the same rule in {deny_label}"
        for rule in allow
        if rule in denied
    ]


def lint_all(
    *, allow: list[str], deny: list[str], ask: list[str] | None = None, label: str, hide: Iterable[str] = ()
) -> list[str]:
    hide = list(hide)
    problems = lint(allow, label=f"{label} allow", hide=hide) + lint(deny, label=f"{label} deny", hide=hide)
    if ask is not None:
        problems += lint(ask, label=f"{label} ask", hide=hide)
    return problems + shadowed(allow, deny, hide)


_COMPOUND_OPERATOR_RE = re.compile("\\|\\&|&&|\\|\\||[;&|]|\\n")


def split_compound(command: str) -> list[str]:
    masked = mask_quotes(command)
    parts: list[str] = []
    last = 0
    for m in _COMPOUND_OPERATOR_RE.finditer(masked):
        parts.append(command[last : m.start()])
        last = m.end()
    parts.append(command[last:])
    return [p.strip() for p in parts if p.strip()]


def _wildcard_pattern(text: str) -> str:
    return ".*".join((re.escape(part) for part in text.split("*")))


def _bash_regex(arg: str) -> re.Pattern[str]:
    if "*" not in arg:
        return re.compile(f"^{re.escape(arg)}$")
    if arg.endswith(":*"):
        prefix = arg[:-2]
        body = re.escape(prefix) if "*" in prefix else _wildcard_pattern(prefix)
        return re.compile(f"^{body}(?:\\s.*)?$")
    return re.compile(f"^{_wildcard_pattern(arg)}$")


def _rule_matches(rule: str, command: str) -> bool:
    match = RULE_RE.match(rule.strip())
    if not match or match.group("tool") != "Bash":
        return False
    arg = match.group("arg")
    if arg is None:
        return True
    return _bash_regex(arg).match(command) is not None


def _part_verdict(command: str, allow: list[str], deny: list[str]) -> tuple[str, str | None]:
    for rule in deny:
        if _rule_matches(rule, command):
            return ("denied", rule)
    for rule in allow:
        if _rule_matches(rule, command):
            return ("allowed", rule)
    return ("no-rule", None)


def matches(command: str, allow: list[str], deny: list[str]) -> tuple[str, str | None]:
    parts = split_compound(command) or [command.strip()]
    verdicts = [_part_verdict(part, allow, deny) for part in parts]
    for verdict, rule in verdicts:
        if verdict == "denied":
            return ("denied", rule)
    if all((verdict == "allowed" for verdict, _ in verdicts)):
        return ("allowed", verdicts[0][1])
    return ("no-rule", None)


_GLOB_CHARS = frozenset("*?[")


def mcp_rule_matches(rule: str, tool: str) -> bool:
    return rule.startswith("mcp__") and fnmatch.fnmatchcase(tool, rule)


def mcp_verdict(tool: str, allow: list[str], deny: list[str]) -> tuple[str, str | None]:
    for rule in deny:
        if mcp_rule_matches(rule, tool):
            return ("denied", rule)
    for rule in allow:
        if mcp_rule_matches(rule, tool):
            return ("allowed", rule)
    return ("no-rule", None)


def mcp_canonical(server: str, rule: str) -> bool:
    prefix = f"mcp__{server}__"
    parsed = RULE_RE.fullmatch(rule)
    if not rule.startswith(prefix) or not parsed or parsed.group("arg") is not None:
        return False
    tool = rule[len(prefix) :]
    return tool == "*" or (bool(tool) and (not _GLOB_CHARS & set(tool)))


def mcp_server_ambiguous(server: str) -> bool:
    return "__" in f"{server}_"


def mcp_selection_problems(
    selected: Iterable[str], where: str = "repo_mcp", fix: str = "drop it from repo_mcp"
) -> list[str]:
    return [
        f"{where} selects {_label(server)!r}, whose name runs into the MCP rule delimiter `__`, so no mcp__<server>__<tool> rule says which server it grants; rename it or {fix}"
        for server in selected
        if mcp_server_ambiguous(server)
    ]


def _mcp_rule_problem(
    server: str, rule: str, where: str, owner: str, hide: list[str], beside: str = ""
) -> str:
    label = _label(server)
    shown = shown_rule(rule, hide) if label == server else REDACTED_MCP_RULE
    whole = f"mcp__{server}__*"
    if label != server or shown_rule(whole, hide) != repr(whole) or (not mcp_canonical(server, whole)):
        whole = "mcp__<server>__*"
    cause = (
        "is a bare server rule, which Claude reads as the whole server and the Codex hook matches to no tool"
        if rule == f"mcp__{server}"
        else f"is not an exact tool or wildcard rule for {owner} {label!r}"
    )
    if rule != rule.strip():
        cause = f"has whitespace around it, so it {cause}"
    return f"{where} {shown} {cause}{beside}; write `{whole}` for the whole server or mcp__<server>__<tool> per tool"


def mcp_profile_problems(
    server: str, allow: list[str], deny: list[str], where: str, hide: Iterable[str] = ()
) -> list[str]:
    hide = list(hide)
    return [
        _mcp_rule_problem(server, rule, f"{where} {side}", "the profile's server", hide)
        for side, rules in (("allow", allow), ("deny", deny))
        for rule in rules
        if rule.strip().startswith("mcp__") and (not mcp_canonical(server, rule))
    ]


def mcp_grant_problems(
    server: str, allow: list[str], own: list[str], where: str, hide: Iterable[str] = ()
) -> list[str]:
    if mcp_server_grant(server, allow)[0] not in ("mixed", "bare", "other"):
        return []
    hide = list(hide)
    valid = next((rule for rule in allow if mcp_canonical(server, rule)), None)
    beside = f", beside the grant {shown_rule(valid, hide)}" if valid else ""
    return [
        _mcp_rule_problem(server, rule, where, "the selected server", hide, beside)
        for rule in dict.fromkeys(_mcp_invalid(server, allow))
        if rule in own and (valid or RULE_RE.match(rule.strip()))
    ]


def _mcp_invalid(server: str, allow: list[str]) -> list[str]:
    probe = f"mcp__{server}__x"
    return [
        rule
        for rule, body in ((rule, rule.strip()) for rule in allow)
        if not mcp_canonical(server, rule)
        and (
            body == f"mcp__{server}"
            or body.startswith(f"mcp__{server}__")
            or (_GLOB_CHARS & set(body) and mcp_rule_matches(body, probe))
        )
    ]


def mcp_server_grant(
    server: str, allow: list[str], deny: list[str] | tuple[str, ...] = ()
) -> tuple[str, list[str]]:
    if mcp_server_ambiguous(server):
        return ("ambiguous", [])
    canonical = [rule for rule in allow if mcp_canonical(server, rule)]
    invalid = _mcp_invalid(server, allow)
    if canonical and invalid:
        return ("mixed", invalid)
    live = [rule for rule in canonical if mcp_verdict(rule, [], list(deny))[0] != "denied"]
    if live:
        return ("granted", live)
    if canonical:
        return ("denied", canonical)
    bare = [rule for rule in invalid if rule == f"mcp__{server}"]
    if bare:
        return ("bare", bare)
    return ("other", invalid) if invalid else ("none", [])
