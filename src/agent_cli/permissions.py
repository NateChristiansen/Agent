"""Lint for Claude Code permission rules.

Four dispatch attempts died today because a rule did not match the command it was written
for: `--json-schema` given a path, `git -C <worktree> ...` against `Bash(git status:*)`,
a worker with no `python3`, and `Bash(gh api repos/:*)` against
`gh api repos/<org>/<repo>/pulls/1/comments`. The last one is the one worth
catching automatically. The rule was not tight, it was dead. A `Bash(<prefix>:*)` rule matches
by prefix, and the prefix has to end on a whole token. A rule that stops mid-argument
matches nothing at all and fails silently. That reads exactly like a deliberate denial.

This module does not reimplement Claude Code's matcher. It flags the shapes that cannot work
and the ones that contradict each other. That is what a human gets wrong.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable

from .quote_mask import mask_quotes

# A rule is `Tool`, `Tool(arg)`, or `Tool(prefix:*)`. MCP tools are named
# `mcp__<server>__<tool>`, where the server may contain dashes and the tool may be `*`.
RULE_RE = re.compile(r"^(?P<tool>[A-Za-z_][A-Za-z0-9_-]*(?:\*)?)(?:\((?P<arg>.*)\))?$")
# A prefix that ends mid-argument: the last character still belongs to a token. `/` is the one
# that bit us. `-`, `=`, `.`, `_` and `,` are the same mistake in a different spelling.
MID_TOKEN_END = ("/", "-", "=", ".", "_", ",", ":")


def _bash_prefix(arg: str) -> str | None:
    """The command prefix a `Bash(...:*)` rule matches on, or None if the rule is exact."""
    return arg[:-2] if arg.endswith(":*") else None


def _stops_mid_token(prefix: str) -> bool:
    """Whether a `Bash(prefix:*)` rule's prefix stops mid-argument instead of on a token
    boundary. This is the shape that made `Bash(gh api repos/:*)` match nothing.

    A prefix containing `*` is doing something deliberate and is never flagged. A prefix
    ending in a bare `--` token is not mid-argument either. `--` is git's own "end of
    options" marker and a complete token by itself, e.g. `Bash(git checkout --:*)` (1.7, the
    worker git widening that lets a worker discard tracked-file edits with `git checkout --
    <path>`). Anything else ending in one of MID_TOKEN_END, including a lone trailing `-` as
    in `-x-`, still stops mid-token.
    """
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
# A URL in any rule, selected server or not: `Bash(curl https://host/token:*)` names an endpoint.
_ENDPOINT_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")
_ENDPOINT_RUN_RE = re.compile(_ENDPOINT_RE.pattern + r"\S*")


def _label(name: str) -> str:
    from .dispatch.mcp_policy import _label  # pyright: ignore[reportPrivateUsage]

    return _label(name)


def _secrets(hide: Iterable[str]) -> list[str]:
    """The names in `hide` that `mcp_policy._label` would not print."""
    return [name for name in hide if name and _label(name) != name]


def shown_rule(rule: str, hide: Iterable[str] = ()) -> str:
    """`rule` quoted for a diagnostic, or a marker in its place.

    An `mcp__` rule that does not parse as a bare tool name is never printed: its segments are
    unvalidated, and a repo may name a server by its endpoint. A rule holding a URL, or a name in
    `hide` that `mcp_policy._label` redacts, is never printed either. The whole rule is withheld
    rather than the name replaced, so overlapping names leave no suffix behind in any order."""
    body = rule.strip()
    if body.startswith("mcp__"):
        parsed = RULE_RE.fullmatch(body)
        if parsed is None or parsed.group("arg") is not None:
            return REDACTED_MCP_RULE
    if _ENDPOINT_RE.search(rule) or any(name in rule for name in _secrets(hide)):
        return REDACTED_RULE
    return repr(rule)


def shown_tool(tool: str) -> str:
    """A tool name as a permission event or telemetry may store it. A name holding text
    `mcp_policy._label` would not print is withheld whole: a server named by its endpoint puts
    that endpoint, path segments and all, into every one of its tool names. Matching always uses
    the raw name; this is display only."""
    if _label(tool) == tool:
        return tool
    return REDACTED_MCP_TOOL if tool.startswith("mcp__") else REDACTED_TOOL


def shown_text(text: str) -> str:
    """Free text (a reason, a hint, a quoted command) as a diagnostic, record or log may show it:
    each endpoint, from its scheme to the next whitespace, is withheld, so no host, path or suffix
    survives. The words around it stay. Matching and execution always use the raw text."""
    return _ENDPOINT_RUN_RE.sub(REDACTED_ENDPOINT, text)


def _fix(rule: str, shown: str, suggestion: str, withheld: str, hide: list[str]) -> str:
    """`suggestion` in backticks, or the `withheld` advice when `rule` or the suggestion may not
    be printed."""
    if shown != repr(rule) or shown_rule(suggestion, hide) != repr(suggestion):
        return withheld
    return f"`{suggestion}`"


def lint(rules: list[str], *, label: str, hide: Iterable[str] = ()) -> list[str]:
    """Problems with a permission list, worst first. Empty means nothing obviously wrong.

    Catches: unparseable rules, duplicates, `Bash(...:*)` prefixes that stop mid-token, and
    `*` mixed with a trailing `:*`, none of which can match a real command. Every rule is
    printed through `shown_rule`; `hide` holds the selected MCP server names.
    """
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
                f"{label}: {shown} mixes `*` with the trailing `:*`, so Claude Code reads the whole "
                f"rule as a literal prefix and it matches nothing. Write {fix}."
            )
            continue
        if _stops_mid_token(prefix):
            whole = f"Bash({prefix.rstrip(''.join(MID_TOKEN_END))}:*)"
            fix = _fix(rule, shown, whole, "a prefix that ends on a whole token", hide)
            problems.append(
                f"{label}: {shown} stops mid-token, so it matches nothing. A prefix rule has to end on a "
                f"whole token: write {fix} or spell the command out."
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
    """Allow rules that a deny rule cancels outright. Deny wins, so an allow that is exactly
    denied elsewhere is a contradiction someone will read as working. The labels say which list
    each side came from when the two are kept apart."""
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


# lint detects syntactically dead rules; matches scores a concrete command using Claude Bash-rule
# semantics. Preflight must check both so missing coverage is visible before a worker launches.

_COMPOUND_OPERATOR_RE = re.compile(r"\|\&|&&|\|\||[;&|]|\n")


def split_compound(command: str) -> list[str]:
    """A shell command split into its parts on `&&`, `||`, `;`, `|`, `|&`, `&` and newlines,
    outside quotes. A simple quote-aware splitter, not a shell parser. It does not special-case
    heredocs or command substitution the way `hooks.is_chained` does. This is scoring
    candidate commands pulled out of a work order's prose (`dispatch.extract_candidate_commands`),
    not deciding whether to block a live Bash call."""
    masked = mask_quotes(command)
    parts: list[str] = []
    last = 0
    for m in _COMPOUND_OPERATOR_RE.finditer(masked):
        parts.append(command[last : m.start()])
        last = m.end()
    parts.append(command[last:])
    return [p.strip() for p in parts if p.strip()]


def _wildcard_pattern(text: str) -> str:
    """`text` with every literal `*` turned into `.*` and everything else escaped."""
    return ".*".join(re.escape(part) for part in text.split("*"))


def _bash_regex(arg: str) -> re.Pattern[str]:
    """Anchored regex for one `Bash(...)` rule argument, per Claude Code's documented Bash
    matching (verified against the permissions docs):

    - no `*` at all: exact match of the whole command.
    - a trailing `:*` is shorthand for `Bash(prefix *)`: the bare prefix, or the prefix
      followed by a space and anything. Matching stops on a whole token. That is exactly why
      `Bash(gh api repos/:*)` never matched `gh api repos/<org>/...` (`/` is not a token
      boundary) while `Bash(gh api:*)` matches it.
    - a bare `*` anywhere else is a wildcard matching any text, including spaces.
    - a `*` before a trailing `:*` is not a wildcard: Claude Code reads such a rule as one
      literal prefix and warns at startup that it never matches (seen on 2.1.283). This matcher agreed with the rule's intent instead, so `Bash(git * push:*)`
      passed as a working worker deny while `git -C x push` went through it.
    """
    if "*" not in arg:
        return re.compile(f"^{re.escape(arg)}$")
    if arg.endswith(":*"):
        prefix = arg[:-2]
        body = re.escape(prefix) if "*" in prefix else _wildcard_pattern(prefix)
        return re.compile(f"^{body}(?:\\s.*)?$", re.DOTALL)
    return re.compile(f"^{_wildcard_pattern(arg)}$", re.DOTALL)


def _rule_matches(rule: str, command: str) -> bool:
    match = RULE_RE.match(rule.strip())
    if not match or match.group("tool") != "Bash":
        return False
    arg = match.group("arg")
    if arg is None:
        return True  # bare `Bash` (no parens) grants every command
    return _bash_regex(arg).match(command) is not None


def _part_verdict(command: str, allow: list[str], deny: list[str]) -> tuple[str, str | None]:
    for rule in deny:
        if _rule_matches(rule, command):
            return "denied", rule
    for rule in allow:
        if _rule_matches(rule, command):
            return "allowed", rule
    return "no-rule", None


def matches(command: str, allow: list[str], deny: list[str]) -> tuple[str, str | None]:
    """Claude Code's documented Bash-permission verdict for `command`:
    `("allowed" | "denied" | "no-rule", the matching rule or None)`.

    A rule for a different tool (`Edit(...)`, `mcp__x__y`) never matches a shell command
    string and is silently skipped. Only `Bash(...)` rules apply here. A compound command
    (`&&`, `||`, `;`, `|`, `|&`, `&`, newlines, outside quotes: see `split_compound`) is
    scored part by part. Deny wins over allow on any single part, and every part has to be
    allowed for the whole to verdict `allowed`.
    """
    parts = split_compound(command) or [command.strip()]
    verdicts = [_part_verdict(part, allow, deny) for part in parts]
    for verdict, rule in verdicts:
        if verdict == "denied":
            return "denied", rule
    if all(verdict == "allowed" for verdict, _ in verdicts):
        return "allowed", verdicts[0][1]
    return "no-rule", None


# ------------------------------------------------------------------ MCP grants (both providers)
#
# Claude Code matches MCP rules natively; the Codex worker hook matches them with `mcp_verdict`
# below. The two agree on an exact `mcp__<server>__<tool>` and on `mcp__<server>__*`. They do not
# agree on a bare `mcp__<server>`: Claude reads it as the whole server, while the Codex hook
# compares it with a full tool name and never matches. `mcp_server_grant` holds a repo's
# grants, and `mcp_profile_problems` a worker profile's rules, to the two spellings both
# providers honour.

_GLOB_CHARS = frozenset("*?[")


def mcp_rule_matches(rule: str, tool: str) -> bool:
    """Whether one `mcp__...` rule covers the tool name `tool` in the Codex hook."""
    return rule.startswith("mcp__") and fnmatch.fnmatchcase(tool, rule)


def mcp_verdict(tool: str, allow: list[str], deny: list[str]) -> tuple[str, str | None]:
    """`("denied" | "allowed" | "no-rule", rule)` for an MCP tool call. Deny wins."""
    for rule in deny:
        if mcp_rule_matches(rule, tool):
            return "denied", rule
    for rule in allow:
        if mcp_rule_matches(rule, tool):
            return "allowed", rule
    return "no-rule", None


def mcp_canonical(server: str, rule: str) -> bool:
    """`mcp__<server>__*` or an exact `mcp__<server>__<tool>` with no glob characters, and a rule
    `lint` accepts as written: settings assembly refuses one it does not."""
    prefix = f"mcp__{server}__"
    parsed = RULE_RE.fullmatch(rule)
    if not rule.startswith(prefix) or not parsed or parsed.group("arg") is not None:
        return False
    tool = rule[len(prefix) :]
    return tool == "*" or (bool(tool) and not _GLOB_CHARS & set(tool))


def mcp_server_ambiguous(server: str) -> bool:
    """Whether a rule cannot say it names `server` rather than another server: the name holds
    the `__` delimiter, or ends in `_` and runs into it. `mcp__a__b__read` reads as tool
    `b__read` of `a` and as tool `read` of `a__b`; `mcp__a___read` reads as `a` and as `a_`.
    Which server a provider attributes such a call to is not established here."""
    return "__" in f"{server}_"


def mcp_selection_problems(
    selected: Iterable[str], where: str = "repo_mcp", fix: str = "drop it from repo_mcp"
) -> list[str]:
    """Why each selected server `mcp_server_ambiguous` refuses cannot be granted, naming it only
    as `mcp_policy._label` would. `where` names the selection: a repo's `repo_mcp`, or the
    `dispatch.mcp` profile that brings the server."""
    return [
        f"{where} selects {_label(server)!r}, whose name runs into the MCP rule delimiter `__`, so no "
        f"mcp__<server>__<tool> rule says which server it grants; rename it or {fix}"
        for server in selected
        if mcp_server_ambiguous(server)
    ]


def _mcp_rule_problem(
    server: str, rule: str, where: str, owner: str, hide: list[str], beside: str = ""
) -> str:
    """Why `rule` is not `mcp_canonical` for `server`, the fix both providers read alike, and,
    in `beside`, the valid grant it sits next to. A rule naming a server whose name is withheld is
    withheld too: a pattern such as `mcp__https*` spells the start of that name."""
    label = _label(server)
    shown = shown_rule(rule, hide) if label == server else REDACTED_MCP_RULE
    whole = f"mcp__{server}__*"
    if label != server or shown_rule(whole, hide) != repr(whole) or not mcp_canonical(server, whole):
        whole = "mcp__<server>__*"
    cause = (
        "is a bare server rule, which Claude reads as the whole server and the Codex hook matches to no tool"
        if rule == f"mcp__{server}"
        else f"is not an exact tool or wildcard rule for {owner} {label!r}"
    )
    if rule != rule.strip():
        cause = f"has whitespace around it, so it {cause}"
    return (
        f"{where} {shown} {cause}{beside}; write `{whole}` for the whole server "
        "or mcp__<server>__<tool> per tool"
    )


def mcp_profile_problems(
    server: str, allow: list[str], deny: list[str], where: str, hide: Iterable[str] = ()
) -> list[str]:
    """Why each `mcp__` rule a worker MCP profile allows or denies is not `mcp_canonical` for the
    profile's own `server`. A bare `mcp__<server>` is the whole server to Claude and no tool to the
    Codex hook, in deny as in allow, so it refuses rather than being widened; so does a pattern or a
    rule naming another server. Other rules are `lint`'s."""
    hide = list(hide)
    return [
        _mcp_rule_problem(server, rule, f"{where} {side}", "the profile's server", hide)
        for side, rules in (("allow", allow), ("deny", deny))
        for rule in rules
        if rule.strip().startswith("mcp__") and not mcp_canonical(server, rule)
    ]


def mcp_grant_problems(
    server: str, allow: list[str], own: list[str], where: str, hide: Iterable[str] = ()
) -> list[str]:
    """Why each rule in `own` that names `server` without being `mcp_canonical` refuses the launch.
    Claude reads a bare rule as the whole server and the Codex hook as no tool, alone or beside a
    canonical grant in `allow` (every allow rule a dispatch gives the server), which would hide two
    providers granting different tools. With no canonical grant, a rule `lint` refuses keeps its
    own line only. No rule naming the server is ordinary absence, and a grant deny cancels is
    deny's priority; neither refuses here."""
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
    """The rules in `allow` that name `server` without being `mcp_canonical`. A rule is read
    stripped only to see which server it names: `lint` strips before it parses, so whitespace
    around a rule must not carry it past this check, and the rule itself is never trimmed."""
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
    """How `allow` grants MCP server `server` to a worker on either provider.

    `("ambiguous", [])`: `mcp_server_ambiguous`; no rule grants it, whatever `allow` holds.
    `("mixed", rules)`: canonical rules beside rules that name the server some other way (a bare
    `mcp__<server>`, a glob), which are returned. The valid grant excuses none of them, cancelled
    or not.
    `("granted", rules)`: at least one canonical rule that no deny rule cancels.
    `("denied", rules)`: canonical rules exist, and deny cancels every one of them.
    `("bare", rules)`: only a bare `mcp__<server>`, which Claude honours and Codex does not.
    `("other", rules)`: only spellings that are not canonical (`mcp__<server>*`,
    `mcp__<server>__read_*`); neither provider is known to read them the same way.
    `("none", [])`: nothing names the server.
    `mcp_grant_problems` refuses a launch on `mixed`, `bare` and `other`.
    A canonical rule is cancelled when a deny rule matches it read as a tool name, so
    `mcp__<server>__*` in deny cancels the wildcard grant while one denied tool does not.
    """
    if mcp_server_ambiguous(server):
        return "ambiguous", []
    canonical = [rule for rule in allow if mcp_canonical(server, rule)]
    invalid = _mcp_invalid(server, allow)
    if canonical and invalid:
        return "mixed", invalid
    live = [rule for rule in canonical if mcp_verdict(rule, [], list(deny))[0] != "denied"]
    if live:
        return "granted", live
    if canonical:
        return "denied", canonical
    bare = [rule for rule in invalid if rule == f"mcp__{server}"]
    if bare:
        return "bare", bare
    return ("other", invalid) if invalid else ("none", [])
