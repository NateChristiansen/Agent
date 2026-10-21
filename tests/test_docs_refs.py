"""Every name a doc cites exists in the code.

The docs were once audited by hand: five read-only shards and a fix pass for drift a script
finds in seconds, and the fix itself carried four new false claims. Its scope left out the text a
model reads at runtime (tool descriptions, help, deny strings) and docstrings, which is where the
drift had moved (@@ to @@). This test makes the audit a standing check, so a rename that
orphans a doc fails in the scoped selection of the commit that made it.

The inventory comes from the code, never from a hand list:

- `agent` commands, subcommands and flags: the argparse tree (`cli.build_parser`), and each
  example is parsed by that parser itself;
- MCP tools and their actions: the tools `mcp.server.build_server` lists, per profile, and each
  tool's `action` enum;
- hook and group names: `hooks.run.NAMES`, `hooks.run.ALIASES` and `hooks.groups.GROUPS`;
- `module.name`: the AST of that module (what it defines or imports);
- repo paths: the files on disk, and `notes.PRIVATE_SET` for the paths a notes repo links in.

What is read:

- Markdown: AGENTS.md, README.md, CONTRIBUTING.md, SECURITY.md, `.agents/skills/**`,
  `.claude/**`, `harness/**`, `docs/**` and the ADR index, including frontmatter and code fences.
  ADR bodies are history and are not read: a retired name there is the record of its retirement
  (ADR-0058, ADR-0059);
- every MCP tool description, as a client sees it;
- the hook commands wired in `.claude/settings.json` and `.codex/hooks.json`;
- every string constant in `src/` (help, deny and error text, docstrings) for `agent` commands,
  hook names and MCP tools, and module docstrings for repo paths and `module.name` references.

Hook names and `x.py` names count inside backticks, code fences and frontmatter, where a doc means
them literally; repo paths and MCP action names count in running text too. An MCP tool name counts
when it is written as code, alone or in a list, before "tool(s)" or "MCP tool(s)" in any case,
or is a bare snake_case name there (`TOOL_RUN`); a bare English word before "tool" is a known
residual, so docs write tool names in backticks. Every MCP reference is read from one normalized
text per block (`reference_text`: markup dropped, code spans kept as code). An action name counts
after "action(s)" and a separator, every name of a list of them (`` `a`, `b` or `c` ``). Known
residuals: an action named after other words ("its action is `land`"), a name in inline HTML
(`<code>x</code> tool`), a phrase a colon breaks (`` `x`: MCP tool ``), and a keyword word used
as an action name inside a list (`` actions `doctor`, `tools` ``). Commands count
in backticks and in shell fences (`sh`, `bash`, `console`, unlabelled; a `text` fence is prose),
found by markdown-it in any container (a list, a blockquote, a table):
every line there that names the launcher must parse in a strict grammar (`parse_line`) or fails
as not checkable.
Spans holding a placeholder (`<id>`, `NNN`, `{x}`, `...`) are examples and are skipped. `ALLOWED`
is the short list of literal mentions that are right to name something absent, with reasons.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import functools
import io
import json
import math
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

import anyio
import pytest
import yaml
from markdown_it import MarkdownIt

from agent_cli import notes
from agent_cli.cli import build_parser
from agent_cli.dispatch import settings as dispatch_settings
from agent_cli.hooks import groups, toggles
from agent_cli.hooks.run import ALIASES, NAMES
from agent_cli.mcp import server as mcp_server
from agent_cli.mcp.tools import Ctx

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "agent_cli"

MARKDOWN_GLOBS = (
    "AGENTS.md", "README.md", "CONTRIBUTING.md", "SECURITY.md", "adrs/README.md",
    ".agents/skills/**/*.md", ".claude/**/*.md", "harness/**/*.md", "docs/**/*.md",
)  # fmt: skip
HOOK_CONFIGS = (".claude/settings.json", ".codex/hooks.json")
#: Vendored skills (`LICENSE-obsidian-skills`) that teach Obsidian's own syntax, not this harness's names.
FOREIGN = ("obsidian-bases", "obsidian-markdown")

#: (file, reference) pairs that rightly name something that does not exist. Keep each justified.
ALLOWED: dict[tuple[str, str], str] = {
    ("src/agent_cli/hooks/toggles.py", "*"): "RETIRED names removed hooks, so old config stays valid",
    ("src/agent_cli/hooks/run.py", "*"): "ALIASES names removed hooks on purpose, for live sessions' wiring",
    ("src/agent_cli/land_followups.py", "pairing.py"): "names the retired module it replaced",
}

#: Paths that exist only at runtime or only in the owner's checkout, never in a tracked tree.
RUNTIME_PREFIXES = (".agent/", ".claude/worktrees/", ".claude/settings.local.json", ".codex/config.toml")
PLACEHOLDER = re.compile(r"[<>‹›{}\[\]…*$]|\.\.\.|NNN|nnnn|YYYY|\bx\.py|_x\.py|/x/|\bfoo\b|^(?:cmd|sub)$")
FILE_SUFFIXES = frozenset(
    {"py", "md", "json", "jsonl", "yaml", "toml", "txt", "js", "sh", "ps1", "cmd", "exe"}
)
SPAN = re.compile(r"`([^`\n]+)`")
PERMISSION_RULE = re.compile(r"[A-Z][A-Za-z]*\([^()`]*\)")
#: Fences whose lines are shell commands; any other (`text`, `yaml`, `python`) is not run. An
#: indented code block has no info string, so it counts as an unlabelled fence.
SHELL_FENCES = frozenset({"", "sh", "bash", "shell", "zsh", "console"})
_TOPS = r"(?:src|tests|harness|adrs|docs|bin|scripts|\.agents|\.claude|\.codex)"
REPO_PATH = re.compile(rf"^(?:\./)?({_TOPS}/[\w./@-]*[\w/])")
#: A repo path in running text: not inside a longer path or URL, placeholders kept so they skip.
TEXT_PATH = re.compile(rf"(?<![\w./:@-])(?:\./)?({_TOPS}/[\w./@<>{{}}*-]*[\w/>}}*])")
HOOK_SUFFIX = re.compile(r"[a-z]+(?:-[a-z]+)*-(?:guard|stop|gate|nudge)")
KEBAB = re.compile(r"[a-z]+(?:-[a-z]+)+")
WIKILINK = re.compile(r"!?\[\[([^\]]+)\]\]")
#: Every MCP reference pattern below runs over one normalized text (`reference_text`): a
#: Markdown inline block with emphasis and link markers dropped, link text kept, soft breaks as
#: spaces, and each code span as exactly `` `name` ``, so markup never splits a reference.
#:
#: Tool references: names written as code, one or a list (`` `a`, `b` and `c` ``), directly
#: before `tool(s)` or `MCP tool(s)` in any case; and a bare snake_case name there (`x_y tool`),
#: which cannot be English. Any other bare name is prose to this lint ("the appropriate MCP
#: tool"), so docs write tool names in backticks. Also `mcp__agent__x`.
_CODE_NAME = r"`[A-Za-z][\w-]*`"
_NAME_SEPARATOR = r"(?:\s*,\s*(?:(?:and|or)\s+)?|\s+(?:and|or)\s+|\s*/\s*|\s+)"
TOOL_RUN = re.compile(
    rf"(?P<run>{_CODE_NAME}(?:{_NAME_SEPARATOR}{_CODE_NAME})*)\s+(?P<mcp>MCP\s+)?tools?\b(?![/\w-])"
    r"|(?<![\w./`-])(?P<snake>[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+)\s+(?P<smcp>MCP\s+)?tools?\b(?![/\w-])",
    re.I,
)
MCP_PREFIXED = re.compile(r"\bmcp__agent__([a-z_]+)")
#: Between an action keyword and its value: a space, `=`, `:` or a dash, as a doc writes a field,
#: after the keyword's own closing quote when it is a quoted key (`{"action": "land"}`).
_ACTION_SEP = r"(?:[\"']?\s*[=:—–-]\s*|\s+)"
#: After an action-routed tool's name: "`tool` tool, action `x`", "harness tool has action x",
#: "`dispatch` `action=\"spans\"`". An unquoted action counts after "has/with/takes action", or
#: when it is spelled like one (`never-exists`, `verify_show`), so "the action and" stays prose.
#: The tool names come from the server (`tool_action_pattern`). Every keyword ("action", "tool",
#: "has"...) matches in any case (`Action`, a heading); a name is lowercase, as every action is
#: (a capitalized one, "the action `ArgumentParser.add_subparsers`", is not an MCP action).
ACTION_AFTER_TOOL = (
    r"`?(?: (?i:tool)(?:'s)?:?)?[^.;:\n]{0,25}?"
    r"(?:\b(?i:has|with|takes) (?i:actions?) (?![`\"'])([a-z][\w-]*)"
    rf"|\b(?i:actions?){_ACTION_SEP}(?:[`\"']+([a-z][\w-]*)|([a-z]+[_-][\w-]*)))"
)
#: "action x of the `tool` tool".
ACTION_OF_TOOL = re.compile(
    rf"\b(?i:action){_ACTION_SEP}[`\"']*([a-z][\w-]*)[`\"']* (?i:of the) `?([a-z][a-z_]*)`? (?i:tool)\b"
)
#: "action `x`" with no tool named before it: some tool must have that action.
ACTION_ALONE = re.compile(rf"\b(?i:actions?){_ACTION_SEP}[`\"']+([a-z][\w-]*)")


#: A field: the singular keyword, as code or not, then `:` or `=`, then its value
#: (`` `action`: `send`, `to`: `x` ``, `action: `send``). "actions:" introduces a list instead.
_FIELD = re.compile(r"\b(?i:action)[`\"']?\s*[:=]\s*[`\"']*$")
#: A colon makes the preceding name a field key only when a literal value follows it.
_FIELD_VALUE = re.compile(r"\s*:\s*(?:`|[\"']|[+-]?(?:\d|\.\d)|(?:true|false|null)\b|[\[{]|[A-Za-z_]\w*\s*,)")


def _action_more(quote: str, *, parameters: bool = False) -> re.Pattern[str]:
    """The next name of an action list: joined as `TOOL_RUN` joins tool names, quoted exactly as
    the first was, and not a tool (`` `doctor` and `harness` tools ``). Fields and unparsed code
    also allow semicolon-separated parameters; a parsed mapping's terminator stays a boundary."""
    q = re.escape(quote)
    separator = rf"(?:{_NAME_SEPARATOR}|\s*;\s*)" if parameters else _NAME_SEPARATOR
    return re.compile(rf"{separator}{q}([a-z][\w-]*){q}(?!\s+(?i:MCP\s+)?(?i:tools?)\b)")


def _action_run(text: str, first: re.Match[str], group: int) -> list[tuple[int, str]]:
    """The names of an action list whose first name is `first[group]`, with their positions,
    read from structure:

    - a field (`` `action`: `send`, `to`: `x` ``, "action: `doctor` or `land`"): its value, and
      any further names joined to it; only a comma/semicolon-separated name can be the next
      key (`=` or `:` followed by a value), which begins the other parameters;
    - a prose list (`` actions `new` and `land`: `new` starts a worker ``): every name up to a
      colon, which ends the list;
    - inside one code span that did not parse as a mapping (`_mapping_action`), a name followed
      by `:` or `=` is that code's key only after a comma or semicolon.

    A list only when the first name is quoted and closed (`` `x` ``, `"x"`); a parameter quoted
    differently (`` `action="send"`, `to=<name>` ``) ends it."""
    names = [(first.start(group), first[group])]
    start, end = first.start(group), first.end(group)
    quote = text[start - 1] if start > 0 else ""
    if quote not in ("`", '"', "'") or text[end : end + 1] != quote:
        return names
    in_code = quote != "`" and text.count("`", 0, start) % 2 == 1  # quoted inside one code span
    field = _FIELD.search(text, 0, start) is not None
    more_pattern = _action_more(quote, parameters=field or in_code)
    end += 1
    while not re.match(r"\s*[:=]", text[end:]) and (more := more_pattern.match(text, end)):
        after = text[more.end() :]
        separator = text[end : more.start(1) - 1].strip()
        if separator in (",", ";") and (
            (in_code and re.match(r"\s*[:=]", after))
            or (field and (re.match(r"\s*=", after) or _FIELD_VALUE.match(after)))
        ):
            break  # a parameter key, not an action; prose lists still judge the name
        names.append((more.start(1), more[1]))
        end = more.end()
    return names


def _mapping_action(code: str) -> str | None:
    """The `action` value of one code span that is a JSON object, a Python dict or keyword
    arguments (`{"action": "list", "feature": null}`, `action="send", to="x"`), or None when the
    span is none of those or has no string `action`."""
    if "action" not in code:
        return None
    candidates: list[Any] = []
    with contextlib.suppress(ValueError):
        candidates.append(json.loads(code))
    with contextlib.suppress(ValueError, SyntaxError, MemoryError, RecursionError):
        candidates.append(ast.literal_eval(code))
    with contextlib.suppress(SyntaxError, ValueError, MemoryError, RecursionError):
        call = ast.parse(f"f({code})", mode="eval").body
        if isinstance(call, ast.Call) and not call.args:
            candidates.append(
                {k.arg: k.value.value for k in call.keywords if k.arg and isinstance(k.value, ast.Constant)}
            )
    for value in candidates:
        if isinstance(value, dict) and isinstance(action := value.get("action"), str):  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            return action
    return None


#: "the `harness` tool's `land` action".
TOOL_ACTION_AFTER = re.compile(r"`([a-z][a-z_]*)` (?i:tool's) `([a-z][\w-]*)` (?i:action)")
#: Snake-case tools that belong to a client, not to this harness's MCP server.
CLIENT_TOOLS = frozenset(
    {"apply_patch", "request_user_input", "request_user_input_async", "exec_command", "web_search"}
)


@functools.cache
def mcp_server_name() -> str:
    """The harness's MCP server's own name (`agent`): "the agent MCP tools" names the server."""
    return str(mcp_server.build_server(ROOT, "manager").name)


@functools.cache
def client_tools() -> frozenset[str]:
    """The clients' own tools, as the repo already names them: `CLIENT_TOOLS`, every tool name in
    the hook configs' matchers (`Bash`, `Edit|Write|NotebookEdit`, `AskUserQuestion`...), and
    every tool a worker's permissions allow (`dispatch.settings.BASE_ALLOW`, `Bash(git ...)`)."""
    names = set(CLIENT_TOOLS)
    for config in HOOK_CONFIGS:
        text = (ROOT / config).read_text(encoding="utf-8")
        for matcher in re.findall(r'"matcher":\s*"([^"]*)"', text):
            names.update(re.findall(r"\b[A-Z][A-Za-z]+\b|\b(?!mcp__)[a-z]+_[a-z_]+\b", matcher))
    names.update(m.group() for rule in dispatch_settings.BASE_ALLOW if (m := re.match(r"[A-Z]\w*", rule)))
    return frozenset(names)


# ---------------------------------------------------------------------- the inventory


@functools.cache
def parser() -> argparse.ArgumentParser:
    """The CLI's own parser, unchanged (`judging_parser` is the copy the lint judges with)."""
    return build_parser()


@functools.cache
def hook_names() -> frozenset[str]:
    return frozenset(NAMES) | frozenset(ALIASES) | frozenset(groups.GROUPS)


@functools.cache
def mcp_tools() -> dict[str, tuple[str, frozenset[str]]]:
    """Every tool any profile lists: its description and its `action` values (empty if none)."""
    worker = Ctx(root=ROOT, profile="worker", feature="1", dispatch_id="d-1")
    real = mcp_server.worker_ctx
    mcp_server.worker_ctx = lambda _root: worker  # pyright: ignore[reportAttributeAccessIssue]
    try:

        async def listed(profile: str) -> dict[str, tuple[str, frozenset[str]]]:
            server = mcp_server.build_server(ROOT, profile)
            return {
                t.name: (t.description or "", _actions(t.input_schema)) for t in await server.list_tools()
            }

        out: dict[str, tuple[str, frozenset[str]]] = {}
        for profile in sorted(mcp_server.PROFILES):
            out.update(anyio.run(listed, profile))
        return out
    finally:
        mcp_server.worker_ctx = real


def _actions(schema: dict[str, Any]) -> frozenset[str]:
    action = schema.get("properties", {}).get("action")
    if action is None:
        return frozenset()
    if "$ref" in action:
        action = schema.get("$defs", {}).get(str(action["$ref"]).rsplit("/", 1)[-1], {})
    return frozenset(str(v) for v in action.get("enum", [action.get("const")]) if v is not None)


@functools.cache
def all_actions() -> frozenset[str]:
    return frozenset(a for _d, actions in mcp_tools().values() for a in actions)


@functools.cache
def basenames() -> frozenset[str]:
    names: set[str] = set()
    for top in ("src", "tests", "scripts", "bin"):
        names.update(p.name for p in (ROOT / top).rglob("*") if p.is_file())
    return frozenset(names)


@functools.cache
def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


@functools.cache
def modules() -> dict[str, tuple[Path, ...]]:
    """Module name -> the files that can be it: `x.py`, or `x/__init__.py` for a package."""
    found: dict[str, list[Path]] = {}
    for path in SRC.rglob("*.py"):
        name = path.parent.name if path.name == "__init__.py" else path.stem
        found.setdefault(name, []).append(path)
    return {name: tuple(paths) for name, paths in found.items()}


@functools.cache
def bound_names(path: Path) -> frozenset[str]:
    """What a module defines or imports at top level (through `if`/`try`/`with` blocks)."""
    names: set[str] = set()
    pending: list[ast.stmt] = list(_tree(path).body)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                names.update(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
        elif isinstance(node, ast.Import | ast.ImportFrom):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, ast.If | ast.Try | ast.With):
            for block in ("body", "orelse", "finalbody", "handlers"):
                for child in getattr(node, block, []):
                    pending.extend(child.body if isinstance(child, ast.ExceptHandler) else [child])
    return frozenset(names)


@functools.cache
def strings_of(path: Path) -> tuple[tuple[int, str], ...]:
    """Every string constant in one module, with its line: one AST walk per file."""
    return _walked(path)[0]


@functools.cache
def docstrings_of(path: Path) -> tuple[tuple[int, str], ...]:
    """Every docstring in one module (its own, each function's and class's), with its line."""
    return _walked(path)[1]


@functools.cache
def _walked(path: Path) -> tuple[tuple[tuple[int, str], ...], tuple[tuple[int, str], ...]]:
    """One walk per file: its string constants (regex patterns left out: their backticks and
    `agent` are pattern syntax, not an example) and its docstrings, each with only the
    indentation the source added removed (`_source_text`)."""
    strings: list[tuple[int, str]] = []
    docs: list[tuple[int, str]] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    nodes = list(ast.walk(_tree(path)))
    patterns = {
        id(part)
        for node in nodes
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "re"
        and node.args
        for part in ast.walk(node.args[0])
    }
    for node in nodes:
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in patterns:
            strings.append((node.lineno, _source_text(node, lines)))
        elif isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
                and first.value.value
            ):
                docs.append((getattr(node, "lineno", 1), _source_text(first.value, lines)))
    return tuple(strings), tuple(docs)


_TRIPLE_QUOTE = re.compile(r"[rRuUbB]{0,2}(?:\"\"\"|''')")


def _source_text(node: ast.Constant, lines: list[str]) -> str:
    """A string constant's text with the indentation the source added removed, and nothing more.
    A triple-quoted literal's later lines carry the indentation of the line it starts on (a
    docstring: its def or class body's); that much is stripped from each, so an indented code
    block inside it stays one. Any other literal is its text as written."""
    value = str(node.value)
    line = lines[node.lineno - 1] if 0 < node.lineno <= len(lines) else ""
    start = line.encode("utf-8")[node.col_offset :].decode("utf-8", errors="replace")
    indent = line[: len(line) - len(line.lstrip(" \t"))]
    if not indent or not _TRIPLE_QUOTE.match(start) or "\n" not in value:
        return value
    first, *rest = value.split("\n")
    kept = [part[len(indent) :] if part.startswith(indent) else part if part.strip() else "" for part in rest]
    return "\n".join([first, *kept])


@functools.cache
def src_strings() -> frozenset[str]:
    """Every short string constant in `src/`: config keys and event names live here."""
    return frozenset(v for p in SRC.rglob("*.py") for _n, v in strings_of(p) if len(v) < 120)


@functools.cache
def src_text_blob() -> str:
    """Every string constant in `src/` joined, for dotted keys spelled inside longer strings
    (`git config core.attributesFile`)."""
    return "\n".join(v for p in SRC.rglob("*.py") for _n, v in strings_of(p))


# ---------------------------------------------------------------------- the checks


class Word(str):
    """One argument word. `quoted` when it was written in quotes: argument data that is never a
    placeholder or a list of alternatives."""

    quoted: bool

    def __new__(cls, text: str, quoted: bool = False) -> Word:
        word = super().__new__(cls, text)
        word.quoted = quoted
        return word


_ANGLE = re.compile(r"<[^<>\s]+>")
#: The placeholder forms a doc writes for "a value goes here": `<id>`, `<a|b>`, `{x}`, `[x]`,
#: `NNN`, `...`. Recognised only in an unquoted word.
_WORD_PLACEHOLDER = re.compile(
    r"<[^<>\s]+>|\{[^{}\s]*\}|\[[^\[\]\s]*\]|\.\.\.|…|NNN|nnnn|YYYY|\*|^(?:cmd|sub)$"
)


def is_placeholder(word: str) -> bool:
    return not getattr(word, "quoted", False) and bool(_WORD_PLACEHOLDER.search(word))


CANNOT_VERIFY = "cannot verify this example; simplify it"
#: The launcher, written bare (`'agent'` or `a\gent` is refused, not unquoted): `agent`, or a
#: path of `[A-Za-z0-9_./-]` whose last component it is (`bin/agent`, `../bin/agent`,
#: `/abs/bin/agent`). Any other path to it (`./dir:name/agent`, `C:/bin/agent`) is refused.
_LAUNCHER = re.compile(r"(?:[A-Za-z0-9_.-]*/)*agent(?:\.cmd)?|bin\\agent\.cmd")
#: The launcher named: `agent` as a word (not part of a longer name, a label such as
#: `verify:agent`, or a placeholder `<agent>`), or any run ending in `/agent` or `\agent`,
#: whatever comes before it. `agent/016` is a directory, not the last component, and is skipped.
_LAUNCHER_MENTION = re.compile(r"(?:(?<![\w./:<>-])|[/\\])agent(?:\.cmd)?(?![\w./-])")
#: The commands a launcher's output may be piped or chained into. The docs pipe and chain into
#: none today (every launcher line in them is a single command); these are the few the lint's
#: own fixtures use. Any other name in command position (`if`, `then`, `!`, `exec`, `time`,
#: `false`...) fails closed.
COMMANDS_AFTER_LAUNCHER = frozenset({"cat", "grep", "head", "jq", "sort", "tail", "true", "wc"})
#: Where a continuation (a backslash-newline after whitespace) joined two lines: a word break.
_JOIN = "\x00"
_PLAIN = r"[A-Za-z0-9_./:=,@%+{}\[\]-]+"
_SQ = r"'[^'\n\x00]*'"
_DQ = r'"(?:[^"\\$`\n\x00]|\\["\\])*"'
_QUOTED = re.compile(rf"{_SQ}|{_DQ}")
_ASSIGNMENT = re.compile(rf"[A-Za-z_][A-Za-z0-9_]*=(?:{_PLAIN}|{_SQ}|{_DQ})?")
#: `--prompt="two words"`: a plain prefix ending in `=`, then one quoted string.
_PREFIXED = re.compile(rf"([A-Za-z0-9_./:,@%+-]*=)({_SQ}|{_DQ})")
#: A launcher argument may also use the docs' notation: `<id>` placeholders and `a|b` alternatives.
_NOTATION = re.compile(r"[A-Za-z0-9_./:=,@%+<>{}\[\]|-]+")
_PATH = re.compile(r"[A-Za-z0-9_./:,@%+-]+")
_SEPARATORS = frozenset({"|", "&&", "||", ";"})
_FIXED_REDIRECTS = frozenset({"2>&1", "2>/dev/null", ">/dev/null"})


#: The module launcher, the Windows route (`bin/agent.cmd`, `launcher.argv_prefix`): an
#: interpreter written bare (`python`, `python3`, `python.exe`, or a plain path ending in one),
#: an optional `-I`, then `-m agent_cli`. `-m agent_cli.<module>` runs another module, not the CLI.
_PYTHON = re.compile(r"(?:[A-Za-z0-9_.-]*/)*python(?:3(?:\.\d+)?)?(?:\.exe)?")
#: The module launcher named in any spelling, quoted or attached (`-magent_cli`, `"agent_cli"`).
_MODULE_MENTION = re.compile(r"-m\s*agent_cli(?![\w.])")


def _launcher_width(words: list[str]) -> int:
    """How many of `words`' leading tokens are a bare launcher: 1 for `agent` or a path to it,
    3 or 4 for `python [-I] -m agent_cli`, 0 for anything else."""
    if words and _LAUNCHER.fullmatch(words[0]):
        return 1
    if words and _PYTHON.fullmatch(words[0]):
        rest = words[2:] if words[1:2] == ["-I"] else words[1:]
        if rest[:2] == ["-m", "agent_cli"]:
            return len(words) - len(rest) + 2
    return 0


def mentions_launcher(text: str) -> bool:
    """`text` names the launcher, also once quotes and backslashes are dropped (`'agent'`,
    `a\\gent`, `ag""ent`): `agent` as a word, or `-m agent_cli`."""
    stripped = re.sub(r"[\\'\"]", "", text)
    return any(p.search(t) for p in (_LAUNCHER_MENTION, _MODULE_MENTION) for t in (text, stripped))


@dataclass
class ExampleLine:
    """One logical line of an example that names the launcher: where it starts in the example,
    its text, the arguments of each command on it that runs the CLI, and why it cannot be
    verified, if it is outside the strict grammar (`parse_line`)."""

    line: int
    text: str
    argvs: list[list[Word]]
    unverifiable: str | None = None


def example_lines(text: str) -> list[ExampleLine]:
    """Every logical line of `text` (a span, a shell fence's body, a hook command) that names the
    launcher once all backslash-newlines are joined, parsed by `parse_line`.

    A backslash-newline continues a line only after a space or tab; anywhere else (inside a word
    or a name, at a line's start) the line fails closed. A whole-line `#` comment is skipped, and
    is never continued: the shell ends a comment at the newline.

    Lines are judged one by one only when each closes its own quotes. When any line ends inside a
    quote and `text` names the launcher anywhere, the whole of `text` fails closed: a quoted
    string across lines can hide a command (`printf '` then `#' ; agent ...`) or fake one."""
    out: list[ExampleLine] = []
    physical = text.split("\n")
    logical: list[tuple[int, str, bool]] = []  # first physical line, text with joins, broken
    k = 0
    while k < len(physical):
        start, parts, broken = k, list[str](), False
        while True:
            line = physical[k]
            k += 1
            if line.endswith("\\") and k < len(physical) and not line.lstrip().startswith("#"):
                broken = broken or len(line) < 2 or line[-2] not in " \t"
                parts.append(line[:-1] + _JOIN)
                continue
            parts.append(line)
            break
        logical.append((start, "".join(parts), broken))
    if any(_ends_in_quote(joined) for _s, joined, _b in logical) and mentions_launcher(
        text.replace("\\\n", "")
    ):
        shown = next((joined for _s, joined, _b in logical if mentions_launcher(joined)), text)
        return [ExampleLine(0, shown.replace(_JOIN, ""), [], "a quoted string across lines")]
    for start, joined, broken in logical:
        plain = joined.replace(_JOIN, "")
        if not mentions_launcher(plain) or joined.lstrip().startswith("#"):
            continue
        if broken:
            out.append(ExampleLine(start, plain, [], "a line continued inside a word"))
            continue
        body = joined.lstrip(" \t")
        body = body[2:] if body.startswith("$ ") else body  # a console prompt
        parsed = parse_line(body)
        if isinstance(parsed, str):
            out.append(ExampleLine(start, plain, [], parsed))
        else:
            out.append(ExampleLine(start, plain, parsed))
    return out


def _ends_in_quote(line: str) -> bool:
    """`line` ends inside a single- or double-quoted string, read as the shell reads it: a
    backslash outside single quotes escapes the next character, and a `#` starting a word
    outside quotes begins a comment."""
    quote = ""
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if quote == "'":
            quote = "" if c == "'" else quote
        elif c == "\\":
            i += 1
        elif quote == '"':
            quote = "" if c == '"' else quote
        elif c in "'\"":
            quote = c
        elif c == "#" and (i == 0 or line[i - 1] in " \t" + _JOIN):
            return False
        i += 1
    return bool(quote)


def _tokens(line: str) -> list[str]:
    """`line` split at spaces, tabs and joins outside quotes; each token is checked later. A `#`
    starting a token begins a comment, which runs to the end of the line; one that a join
    continues comes back whole, to be refused (the shell ends a comment at the newline)."""
    tokens: list[str] = []
    i, n = 0, len(line)
    while i < n:
        if line[i] in " \t" + _JOIN:
            i += 1
            continue
        if line[i] == "#":
            if _JOIN in line[i:]:
                tokens.append(line[i:])
            break
        j = i
        while j < n and line[j] not in " \t" + _JOIN:
            if line[j] == "'":
                close = line.find("'", j + 1)
                j = n if close < 0 else close + 1
            elif line[j] == '"':
                j += 1
                while j < n and line[j] != '"':
                    j += 2 if line[j] == "\\" else 1
                j += 1
            else:
                j += 1
        tokens.append(line[i : min(j, n)])
        i = j
    return tokens


def _unquote(token: str) -> str:
    inner = token[1:-1]
    return re.sub(r'\\(["\\])', r"\1", inner) if token.startswith('"') else inner


def _word(token: str, notation: bool) -> Word | None:
    """`token` as one argument, or None when it is outside the grammar: a plain word, one quoted
    string (empty included), or a plain `name=` prefix and one quoted string. With `notation`
    (a launcher argument) it may also hold `<placeholder>`s and `a|b` alternatives, recorded as
    `¦`; a `<` or `>` outside a placeholder, or an empty alternative, is refused."""
    if _QUOTED.fullmatch(token):
        return Word(_unquote(token), quoted=True)
    if m := _PREFIXED.fullmatch(token):
        return Word(m.group(1) + _unquote(m.group(2)), quoted=True)
    if re.fullmatch(_PLAIN, token):
        return Word(token)
    if not notation or not _NOTATION.fullmatch(token):
        return None
    pieces = re.split(f"({_ANGLE.pattern})", token)
    outside = "".join(p for p in pieces[::2])
    if "<" in outside or ">" in outside:
        return None
    if not all(_ANGLE.sub("x", token).split("|")):
        return None
    return Word("".join(p if i % 2 else p.replace("|", "¦") for i, p in enumerate(pieces)))


def parse_line(line: str) -> list[list[Word]] | str:
    """The arguments of each command on `line` that runs the CLI, or why the line is outside the
    strict grammar. The grammar is a whitelist; anything else fails closed:

    - commands separated by ` | `, ` && `, ` || ` or ` ; ` (spaces on both sides), then an
      optional ` # comment`;
    - a command is `NAME=value` assignments (the value a plain word or one quoted string), then
      a name, its words, and at most one trailing redirect: `> PATH`, `>PATH`, `2>&1`,
      `2>/dev/null` or `>/dev/null`;
    - a command runs the CLI when its name, after an optional `uv run`, is a bare launcher;
      its arguments are plain words (with the docs' notation, `_word`), single-quoted strings,
      or double-quoted strings whose only escapes are `\\"` and `\\\\` (no `$`, no backtick);
    - a command that runs the CLI starts the line or follows ` ; `, so it always runs: one after
      ` && `, ` || ` or ` | ` would run only when another command lets it;
    - any other command follows one that runs the CLI, is named in `COMMANDS_AFTER_LAUNCHER`
      (no reserved word, no `exec`), and has plain or quoted words that do not name the launcher;
    - the line must run the CLI somewhere."""
    groups: list[list[str]] = [[]]
    operators: list[str | None] = [None]  # the separator before each command
    for token in _tokens(line):
        if token.startswith("#"):
            return "a comment continued onto the next line"
        if token in _SEPARATORS:
            if not groups[-1]:
                return "an empty command"
            groups.append([])
            operators.append(token)
        else:
            groups[-1].append(token)
    if not groups[-1]:
        return "a trailing operator" if len(groups) > 1 else "an empty command"
    argvs: list[list[Word]] = []
    for operator, group in zip(operators, groups, strict=True):
        if len(group) >= 2 and group[-2] == ">" and _PATH.fullmatch(group[-1]):
            group = group[:-2]
        elif group[-1] in _FIXED_REDIRECTS or (group[-1][:1] == ">" and _PATH.fullmatch(group[-1][1:])):
            group = group[:-1]
        i = 0
        while i < len(group) and _ASSIGNMENT.fullmatch(group[i]):
            if group[i].startswith("PATH="):
                return "an assignment to PATH, which changes what runs"
            i += 1
        rest = group[i:]
        if not rest:
            return "a command with no name"
        at = 2 if rest[:2] == ["uv", "run"] else 0
        width = _launcher_width(rest[at:])
        if width:
            if operator not in (None, ";"):
                return f"the launcher run only when ` {operator} ` lets it"
            argv: list[Word] = []
            for token in rest[at + width :]:
                if (word := _word(token, notation=True)) is None:
                    return f"`{token}` is not a plain word or a simple quoted string"
                argv.append(word)
            argvs.append(argv)
            continue
        if rest[0] not in COMMANDS_AFTER_LAUNCHER:
            return f"`{rest[0]}` is not a command this lint knows"
        if not argvs:
            return f"`{rest[0]}` runs before the launcher"
        words: list[Word] = []
        for token in rest:
            if (word := _word(token, notation=False)) is None:
                return f"`{token}` is not a plain word or a simple quoted string"
            words.append(word)
        if any(mentions_launcher(w) for w in words):
            return "the launcher run through another command"
    if not argvs:
        return "names the launcher without running it as a plain command"
    return argvs


def check_command(text: str) -> str | None:
    """Why `agent <text>` does not resolve, or None when it does. The real parser is the only
    judge (`_parse`): every error it raises fails the example, except a missing required
    argument, which is filled with a dummy so the rest is still judged.

    `a|b` in any position (a subcommand, an option value, a hook name) lists alternatives: the
    example expands to one whole command per combination, and each must pass. An example with
    more combinations than `MAX_EXPANSIONS` fails as not checkable, rather than passing on the
    ones that were checked. An example outside the strict grammar (`parse_line`) fails as not
    checkable."""
    line = example_lines(f"agent {text}")[0]
    if line.unverifiable:
        return f"{CANNOT_VERIFY} ({line.unverifiable})"
    return check_argv(line.argvs[0])


def check_argv(words: list[Word]) -> str | None:
    """`check_command` for words already split (the arguments after `agent`)."""
    total = math.prod(1 if word.quoted else word.count("¦") + 1 for word in words)
    if total > MAX_EXPANSIONS:
        return f"{total} alternative combinations exceed the {MAX_EXPANSIONS} this lint checks; split the example"
    for expanded in _expansions(words):
        if (why := _check_words(expanded)) is not None:
            return why
    return None


#: The most combinations of alternatives one example may have; past it the example fails.
MAX_EXPANSIONS = 64


def _expansions(words: list[Word]) -> list[list[Word]]:
    out: list[list[Word]] = [[]]
    for word in words:
        options = [Word(o) for o in word.split("¦")] if "¦" in word and not word.quoted else [word]
        out = [[*prefix, option] for prefix in out for option in options]
    return out


def _check_words(words: list[Word]) -> str | None:
    """The words as argv, each unquoted placeholder (`<id>`, `--feature=<id>`) replaced by
    `_PLACEHOLDER`, which the judging parser accepts as any value; then `_parse`. Commands are
    only read from code, where every character is literal: `--wait 2:` is checked as `2:`."""
    argv: list[str] = []
    for word in words:  # an empty quoted word is an argument too: `agent '' status`
        flag, eq, value = word.partition("=")
        if word.startswith("-") and eq and is_placeholder(Word(value, word.quoted)):
            argv.append(f"{flag}={_PLACEHOLDER}")
        else:
            argv.append(_PLACEHOLDER if is_placeholder(word) else str(word))
    return _parse(tuple(argv))


#: What a placeholder becomes in argv: never a real value, so the judging parser knows it.
_PLACEHOLDER = "‹placeholder›"
#: How many missing required arguments `_parse` fills before it gives up.
_MAX_FILLS = 8


class _ParseError(Exception):
    def __init__(self, parser: argparse.ArgumentParser, message: str) -> None:
        super().__init__(message)
        self.parser = parser
        self.message = message


def _dummy(action: argparse.Action, convert: Any) -> Any:
    """A value `action` accepts: its first choice, or a dummy its type converts."""
    if action.choices:
        return next(iter(action.choices))  # pyright: ignore[reportUnknownArgumentType]
    for dummy in ("x", "1"):
        with contextlib.suppress(Exception):
            return convert(dummy) if convert is not None else dummy
    return "1"


class _ChoicesWithPlaceholder(dict[str, Any]):
    """A subcommand table that also holds `_PLACEHOLDER` (`dispatch <sub>`)."""

    def __contains__(self, key: object) -> bool:
        return key == _PLACEHOLDER or super().__contains__(key)


class _JudgingSubParsers(argparse._SubParsersAction):  # pyright: ignore[reportPrivateUsage, reportMissingTypeArgument]
    """A placeholder subcommand takes the words after it unjudged: which parser would read them
    is unknown. The words before it have been judged already."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        if values and values[0] == _PLACEHOLDER:
            return
        super().__call__(parser, namespace, values, option_string)


@functools.cache
def judging_parser() -> argparse.ArgumentParser:
    """A fresh copy of the CLI parser (`build_parser`), changed only so it can judge: every
    parser raises `_ParseError` instead of printing and exiting, every argument accepts
    `_PLACEHOLDER` as a valid value of its own (`_JudgingSubParsers` for a subcommand), and no
    subcommand is required. Nothing else is reimplemented: options, prefixes, attached values (`-mx`),
    choices and types are argparse's own."""
    root = build_parser()
    pending = [root]
    while pending:
        current = pending.pop()

        def error(message: str, current: argparse.ArgumentParser = current) -> NoReturn:
            raise _ParseError(current, message)

        current.error = error  # type: ignore[method-assign]
        for action in current._actions:  # pyright: ignore[reportPrivateUsage]
            if isinstance(action, argparse._SubParsersAction):  # pyright: ignore[reportPrivateUsage]
                pending.extend(action.choices.values())  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
                # A missing subcommand is an incomplete example, never an error to fill: the
                # words before it are still judged by this parser (`agent status --nope`).
                action.required = False
                action.__class__ = _JudgingSubParsers
                action.choices = _ChoicesWithPlaceholder(action.choices)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
                continue
            if action.nargs == 0:
                continue
            convert: Any = current._registry_get("type", action.type, action.type)  # pyright: ignore[reportPrivateUsage]
            convert = convert if callable(convert) else None

            def typed(value: str, action: argparse.Action = action, convert: Any = convert) -> Any:
                if value == _PLACEHOLDER:
                    return _dummy(action, convert)
                return convert(value) if convert is not None else value

            typed.__name__ = getattr(convert, "__name__", "str")  # argparse names it in its errors
            action.type = typed
    return root


def _required_name(action: argparse.Action) -> str:
    """The name argparse gives `action` in "the following arguments are required: ..."."""
    if action.option_strings:
        return "/".join(action.option_strings)
    if action.metavar not in (None, argparse.SUPPRESS):
        return str(action.metavar)
    return str(action.dest)


@functools.cache
def _parse(argv: tuple[str, ...]) -> str | None:
    """The real parser's verdict (`judging_parser`). The single exemption: an example names what
    it is about, not every argument. A missing subcommand is not required there, so the words
    present are judged and `agent status` alone passes; when the parser says other required
    arguments are missing, each is filled with a dummy of its type and the example is parsed
    again. Whatever error is left is reported (`agent export <dir> --json`: `--json` belongs
    before the command; `agent status --nope`)."""
    args = list(argv)
    for _attempt in range(_MAX_FILLS):
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                judging_parser().parse_args(args)
        except SystemExit as exc:
            return None if not exc.code else f"exit {exc.code}"  # --help, --version
        except _ParseError as error:
            missing = error.message.partition("the following arguments are required: ")[2]
            if not missing:
                return error.message.replace(_PLACEHOLDER, "<placeholder>")
            names = set(missing.split(", "))
            before = len(args)
            for action in error.parser._actions:  # pyright: ignore[reportPrivateUsage]
                if action.required and _required_name(action) in names:
                    fill = [] if action.nargs == 0 else [_PLACEHOLDER]
                    args += [action.option_strings[0], *fill] if action.option_strings else fill
            if len(args) == before:
                return error.message
            continue
        return None
    return "too many required arguments missing to judge"


def command_problems(text: str) -> list[tuple[int, str]]:
    """Each `agent` command in `text` (a span, a shell fence's body) that fails, with the line of
    `text` it starts on. Detection fails closed: every line that names the launcher is an
    example (`example_lines`), and one outside the strict grammar (`parse_line`) fails as not
    checkable."""
    out: list[tuple[int, str]] = []
    for line in example_lines(text):
        if line.unverifiable:
            shown = " ".join(line.text.split())[:80]
            out.append((line.line, f"`{shown}`: {CANNOT_VERIFY} ({line.unverifiable})"))
            continue
        for argv in line.argvs:
            if (why := check_argv(argv)) is not None:
                out.append((line.line, f"`{' '.join(['agent', *argv])}`: {why}"))
    return out


def check_span(span: str) -> list[str]:
    """Commands and hook names in one backticked span. Paths are `path_problems`'; a fence
    block's commands are read whole (`code_problems`). A client
    permission rule (`Edit(//<parent>/*/bin/agent)`) is not a command: the shell would refuse
    its unquoted parenthesis as a syntax error and run nothing."""
    problems = [] if PERMISSION_RULE.fullmatch(span) else [why for _line, why in command_problems(span)]
    if (why := check_hook_name(span)) is not None:
        problems.append(why)
    return problems


def check_hook_name(span: str) -> str | None:
    """A backticked kebab name shaped like a hook (`-guard`, `-stop`...) or extending a hook's own
    name (`notes-sync-x`) must be a hook or group."""
    span = span.strip()
    if not KEBAB.fullmatch(span) or span in hook_names() or span in toggles.RETIRED:
        return None
    extends = any(span.startswith(f"{name}-") for name in hook_names() if "-" in name)
    if HOOK_SUFFIX.fullmatch(span) or extends:
        return f"`{span}`: no hook or group by that name"
    return None


def _has_reference_words(text: str) -> bool:
    lower = text.lower()
    return "tool" in lower or "action" in lower or "mcp__" in lower


def check_text(text: str) -> list[str]:
    """MCP tool and action names in a string (a docstring, a description, a YAML value): parsed
    as Markdown, each block of running text normalized (`reference_text`) and judged
    (`check_text_at`), as a Markdown file is. Its code blocks are `code_problems`'."""
    if not _has_reference_words(text):
        return []
    return [why for _start, block in _source_code(text).prose for _pos, why in check_text_at(block)]


def code_line_text(line: str) -> list[str]:
    """`check_text` for one line of a code block: read as inline content only (markdown-it
    `parseInline`, leading whitespace dropped), never as block Markdown, so an indented line or a
    nested list inside a fence is judged rather than read as another block; then normalized and
    judged like any running text."""
    stripped = line.strip()
    if not _has_reference_words(stripped):
        return []
    children = _markdown().parseInline(stripped)[0].children or []
    return [why for _pos, why in check_text_at(reference_text(children))]


#: Inline markup that carries no words: dropped by `reference_text` (a link's text is kept).
_MARKUP = frozenset({"em_open", "em_close", "strong_open", "strong_close", "link_open", "link_close"})


#: The words the reference patterns key on, which a doc may also set as code.
_KEYWORDS = frozenset({"action", "actions", "tool", "tools", "tool's", "mcp", "mcp tool", "mcp tools"})
#: Text ending where an action's value goes (`action `, `action: `): what follows is a value.
_AFTER_ACTION_KEY = re.compile(rf"\b(?i:actions?){_ACTION_SEP}$")


def reference_text(children: list[Any]) -> str:
    """One markdown-it inline block as the text every MCP reference pattern reads: text kept,
    soft breaks as newlines (`_unwrapped` reads them as spaces, and they count lines), emphasis
    and link markers dropped, each code span as exactly `` `name` `` and anything else (an image,
    inline HTML) as a space. "**Action** `x`", "[Action](#a) `x`", "`x` **MCP**⏎tool" read as
    the plain phrase. A code span holding only a reference keyword (`` `action` ``,
    `` `MCP tool` ``) is unwrapped to plain text: the keyword is the same word set as code, so
    "`action`: `land`" reads as "action: `land`" for every pattern. A keyword in the value's place
    (`` action `tools` ``) is a value, and stays code."""
    parts: list[str] = []
    for child in children:
        if child.type == "text":
            parts.append(child.content)
        elif child.type in ("softbreak", "hardbreak"):
            parts.append("\n")
        elif (
            child.type == "code_inline"
            and child.content.strip().lower() in _KEYWORDS
            and not _AFTER_ACTION_KEY.search("".join(parts))
        ):
            parts.append(child.content.strip())
        elif child.type == "code_inline":
            parts.append(f"`{child.content}`")
        elif child.type not in _MARKUP:
            parts.append(" ")
    return "".join(parts)


def judge_tool(name: str, mcp: bool) -> str | None:
    """Why `name`, cited as a tool, is not one of this server's tools, or None. A client's own
    tool (`client_tools`) before a plain `tool` is the client's; the server's own name before
    `MCP tools` names the server."""
    if name in mcp_tools() or (not mcp and name in client_tools()) or (mcp and name == mcp_server_name()):
        return None
    return f"`{name}`: no MCP tool by that name"


#: A paragraph break in `_unwrapped` text: not whitespace, not a word character.
_PARAGRAPH = "\n¶\n"


def _unwrapped(text: str) -> tuple[str, list[int]]:
    """`text` with each soft line break (a whitespace run holding one newline) read as one space,
    and a paragraph break (two or more newlines) as `_PARAGRAPH`, which no pattern's `\\s`
    crosses, so a reference wrapped across lines is one phrase and none runs across paragraphs;
    and for each character of the result, its index in `text`."""
    out: list[str] = []
    where: list[int] = []
    last = 0
    for m in re.finditer(r"\s*\n\s*", text):
        out.append(text[last : m.start()])
        where += range(last, m.start())
        gap = _PARAGRAPH if m.group().count("\n") > 1 else " "
        out.append(gap)
        where += [m.start()] * len(gap)
        last = m.end()
    out.append(text[last:])
    where += range(last, len(text))
    return "".join(out), where


def check_text_at(text: str) -> list[tuple[int, str]]:
    """Every MCP reference in one block of normalized running text (`reference_text`), each with
    its index in `text`: tool references (`TOOL_RUN`, every name of a list judged by
    `judge_tool`), `mcp__agent__x`, and action references. Soft line breaks read as spaces
    (`_unwrapped`): a reference wrapped across lines is one."""
    if not _has_reference_words(text):
        return []
    joined, where = _unwrapped(text)
    return [(where[pos] if pos < len(where) else 0, why) for pos, why in _check_joined(joined)]


def _check_joined(text: str) -> list[tuple[int, str]]:
    problems: list[tuple[int, str]] = []
    tools = mcp_tools()
    for m in TOOL_RUN.finditer(text):
        names = re.findall(r"`([^`]+)`", m["run"]) if m["run"] else [m["snake"]]
        mcp = bool(m["mcp"] or m["smcp"])
        problems += [(m.start(), why) for name in names if (why := judge_tool(name, mcp))]
    for m in MCP_PREFIXED.finditer(text):
        if m[1] not in tools:
            problems.append((m.start(), f"`{m[1]}`: no MCP tool by that name"))
    if "action" not in text.lower():
        return problems
    # One code span holding a mapping (`{"action": "list", "feature": null}`, `action="x", to=y`)
    # becomes "action `x`;": the terminator keeps following parameters out of the action list.
    # Restore the exact parsed value at judgement, so spaces or punctuation cannot be truncated
    # by a name pattern. The short replacement fits even `action=""`; padding keeps positions.
    pieces: list[str] = []
    mapping_values: dict[int, str] = {}
    last = 0
    for span in re.finditer(r"`([^`]+)`", text):
        if (action := _mapping_action(span[1])) is not None:
            pieces += [text[last : span.start()], "action `x`;".ljust(len(span[0]))]
            mapping_values[span.start() + len("action `")] = action
            last = span.end()
    text = "".join([*pieces, text[last:]])
    claimed: set[int] = set()
    pairs: list[tuple[int, str, str]] = []
    for m in tool_action_pattern().finditer(text):
        group = next(g for g in (2, 3, 4) if m[g])
        run = _action_run(text, m, group) if group == 3 else [(m.start(group), m[group])]
        # The inserted terminator excludes parameters, but later mapping spans in this
        # sentence inherit the first span's tool until another tool reference intervenes.
        previous = run[-1][0]
        if previous in mapping_values:
            for at in mapping_values:
                if at <= previous:
                    continue
                gap = text[previous + len("x`;") : at - len("action `")]
                if (
                    re.search(r"[.!?\n]", SPAN.sub("", gap))
                    or TOOL_RUN.search(gap)
                    or MCP_PREFIXED.search(gap)
                    or any(span[1] in tools for span in SPAN.finditer(gap))
                    # a bare tool name, as the pairing reads it ("the harness tool", "harness tool's")
                    or any(
                        word in tools for word in re.findall(r"\b(\w+)(?:'s)?\s+(?i:MCP\s+)?(?i:tool)", gap)
                    )
                ):
                    break
                run.append((at, "x"))
                previous = at
        for at, action in run:  # every name of a quoted list: "has actions `new`, `land`"
            pairs.append((m.start(), m[1], mapping_values.get(at, action)))
            claimed.add(at)
    for m in TOOL_ACTION_AFTER.finditer(text):
        pairs.append((m.start(), m[1], m[2]))
        claimed.add(m.start(2))
    for m in ACTION_OF_TOOL.finditer(text):
        pairs.append((m.start(), m[2], m[1]))
        claimed.add(m.start(1))
    for pos, tool, action in pairs:
        if tool in tools and action not in tools[tool][1]:
            problems.append((pos, f"`{tool}` has no action `{action}`"))
    for m in ACTION_ALONE.finditer(text):
        for at, action in _action_run(text, m, 1):  # "action `doctor` or `never_exists`"
            action = mapping_values.get(at, action)
            if at not in claimed and action not in all_actions():
                problems.append((m.start(), f"no MCP tool has action `{action}`"))
    return problems


@functools.cache
def tool_action_pattern() -> re.Pattern[str]:
    routed = sorted((name for name, (_d, actions) in mcp_tools().items() if actions), key=len, reverse=True)
    return re.compile(rf"`?\b({'|'.join(routed)})\b{ACTION_AFTER_TOOL}")


def check_path(token: str, where: Path) -> str | None:
    """Why a repo path or `.py` name does not exist, or None."""
    token = token.strip("'\".,:").removeprefix("vault:")  # a lease area names a vault path
    if not token or PLACEHOLDER.search(token) or token.startswith(RUNTIME_PREFIXES):
        return None
    m = REPO_PATH.match(token)
    if m:
        path = re.sub(r":[\w.-]*$", "", m.group(1)).split("#")[0].rstrip("/")
        if _private(path) or (ROOT / path).exists() or (ROOT / f"{path}.md").exists():
            return None  # `harness/agent-yaml` is how a wikilink-minded doc names the .md
        return f"`{path}`: no such path"
    m = re.fullmatch(r"((?:[a-z_]+/)*[a-z_][a-z0-9_]*\.py)(?::[\w.-]+)?", token)
    if m:
        rel = m.group(1)
        if any((base / rel).exists() for base in (ROOT, SRC, SRC.parent, where.parent, ROOT / "tests")):
            return None
        if "/" not in rel and rel in basenames():
            return None
        return f"`{rel}`: no such file"
    return None


def path_problems(text: str, where: Path, *, literal: bool = False) -> list[str]:
    """The one path check every context uses (Markdown, docstrings, tool descriptions): repo
    paths, relative paths (`doctor/checks_vault.py`) and `x.py` names inside backticks, and repo
    paths in the running text around them. `literal` text (a fence, frontmatter) is meant
    literally throughout, so every word in it is checked as a backticked one is."""
    found: list[str] = []
    spans = [text] if literal else SPAN.findall(text)
    for span in spans:
        found += [why for token in re.split(r"[\s,;()`'\"=]+", span) if (why := check_path(token, where))]
    rest = text if literal else SPAN.sub("", text)
    found += [why for m in TEXT_PATH.finditer(rest) if (why := check_path(m.group(1), where)) is not None]
    return list(dict.fromkeys(found))


def _private(path: str) -> bool:
    return any(path == e.path or path.startswith(e.path + "/") for e in notes.PRIVATE_SET)


def check_symbol(span: str) -> str | None:
    """A docstring's backticked `module.name[.attr]`: `name` is defined in or imported into that
    module (or is its submodule), and `attr` in what `name` is. A config key or event name such
    as `dispatch.max_concurrent` passes when src spells it as a string."""
    m = re.fullmatch(r"([a-z_][a-z0-9_]*)\.([A-Za-z_]\w*)(?:\.([A-Za-z_]\w*))?(?:\(\))?", span)
    if m is None:
        return None
    module, name, attr = m.groups()
    if name in FILE_SUFFIXES or module not in modules():
        return None  # `x.py`, `agent.yaml`: a file name, which `check_path` judges
    owners = [p for p in modules()[module] if _owns(p, name)]
    if not owners:
        if name in src_strings() or f"{module}.{name}" in src_text_blob():
            return None  # a config key or event name, not an attribute
        return f"`{span}`: {module} neither defines nor imports {name}"
    if attr and not any(_owns_attr(p, name, attr) for p in owners):
        if span in src_text_blob():
            return None
        return f"`{span}`: {attr} is not in {module}.{name}"
    return None


def _owns(path: Path, name: str) -> bool:
    if name in bound_names(path):
        return True
    return path.name == "__init__.py" and (
        (path.parent / f"{name}.py").exists() or (path.parent / name).is_dir()
    )


def _owns_attr(path: Path, name: str, attr: str) -> bool:
    if path.name == "__init__.py":
        for sub in (path.parent / f"{name}.py", path.parent / name / "__init__.py"):
            if sub.exists():
                return _owns(sub, attr)  # a submodule: the attribute must be its own
    for node in _tree(path).body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return any(
                attr
                in {n.id for n in ast.walk(stmt) if isinstance(n, ast.Name)} | {getattr(stmt, "name", "")}
                for stmt in node.body
            )
    # An imported module or object: its attributes are not this module's to check.
    return name not in {
        n.name for n in _tree(path).body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
    }


# ---------------------------------------------------------------------- the sources


@functools.cache
def markdown_files() -> tuple[Path, ...]:
    found: set[Path] = set()
    for pattern in MARKDOWN_GLOBS:
        for path in ROOT.glob(pattern):
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(ROOT):
                continue  # a notes-linked file is the notes repo's, not this tree's
            if any(part in FOREIGN for part in path.parts) or ".claude/worktrees" in path.as_posix():
                continue
            found.add(path)
    return tuple(sorted(found))


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else path.name


@dataclass
class MarkdownCode:
    """The code in a Markdown body, from markdown-it's token stream, so a fence or span inside a
    blockquote, list or table is found as CommonMark finds it. Line numbers are 1-based."""

    #: Each fence and indented code block: its first body line, its info word, its body lines.
    blocks: list[tuple[int, str, list[str]]] = field(default_factory=list[tuple[int, str, list[str]]])
    #: Each code span, with the line it is on.
    spans: list[tuple[int, str]] = field(default_factory=list[tuple[int, str]])
    #: A code block's body lines, container prefixes (`> `, list indent) removed.
    body: dict[int, str] = field(default_factory=dict[int, str])
    #: Every line of a code block, its fence lines included.
    lines: set[int] = field(default_factory=set[int])
    #: Each block of running text (a paragraph, heading, list item's or quote's text, table cell)
    #: normalized for MCP references (`reference_text`), or an HTML block's raw text, with the
    #: line it starts on.
    prose: list[tuple[int, str]] = field(default_factory=list[tuple[int, str]])


@functools.cache
def _markdown() -> MarkdownIt:
    return MarkdownIt("commonmark").enable("table")


def markdown_code(text: str) -> MarkdownCode:
    code = MarkdownCode()
    source = text.split("\n")
    for token in _markdown().parse(text):
        if token.map is None:
            continue
        first, end = token.map
        if token.type in ("fence", "code_block"):
            body = token.content.split("\n")[:-1] if token.content else []
            start = first + 2 if token.type == "fence" else first + 1
            info = token.info.split()[0] if token.type == "fence" and token.info.strip() else ""
            code.blocks.append((start, info, body))
            code.body.update({start + k: line for k, line in enumerate(body)})
            code.lines.update(range(first + 1, end + 1))
        elif token.type == "inline":
            code.prose.append((first + 1, reference_text(token.children or [])))
            # A span's line: found in the source in order, since a span can itself cross lines.
            raw, cursor = "\n".join(source[first:end]), 0
            for child in token.children or []:
                if child.type != "code_inline":
                    continue
                # Line breaks inside a span read as spaces; a table writes `|` as `\|`.
                parts = [
                    re.escape(part).replace(r"\|", r"\\?\|") for part in child.content.split(" ") if part
                ]
                tick = re.escape(child.markup)
                pattern = rf"{tick}[\s>]*{r'[\s>]+'.join(parts)}[\s>]*{tick}"
                m = re.compile(pattern).search(raw, cursor) if parts else None
                if m is not None:
                    cursor = m.end()
                code.spans.append(
                    (first + 1 + raw.count("\n", 0, cursor if m is None else m.start()), child.content)
                )
        elif token.type == "html_block":  # not Markdown, but a doc may still quote a command there
            code.prose.append((first + 1, token.content))
            code.spans += [
                (first + 1 + k, s) for k, line in enumerate(source[first:end]) for s in SPAN.findall(line)
            ]
    return code


def frontmatter_problems(text: str, first_line: int) -> list[tuple[int, str]]:
    """Every string value in YAML frontmatter, nested ones and block scalars (`>-`, `|`, plain
    multiline) included, as YAML reads it: its code through `source_code` and `code_problems`,
    the value itself as a hook name, and its MCP tool and action names (`check_text`), so a
    folded line break cannot split a reference. Lines are `first_line` plus the value's offset (a block
    scalar's text starts on the line after its indicator; a folded one's lines are approximate).
    Frontmatter that is not valid YAML fails: its values could not be read."""
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        return [(first_line, f"frontmatter is not valid YAML: {str(exc).splitlines()[0]}")]
    out: list[tuple[int, str]] = []
    pending: list[yaml.Node] = [root] if root is not None else []
    while pending:
        node = pending.pop()
        if isinstance(node, yaml.ScalarNode):
            value = str(node.value)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            start = first_line + node.start_mark.line + (1 if node.style in ("|", ">") else 0)
            for k, whys in code_problems(source_code(value)).items():
                out += [(start + k - 1, why) for why in whys]
            if (why := check_hook_name(value)) is not None:
                out.append((start, why))
            out += [(start, why) for why in check_text(value)]  # MCP tools and actions, unfolded
        elif isinstance(node, yaml.MappingNode):
            pending += [part for pair in node.value for part in pair]  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
        elif isinstance(node, yaml.SequenceNode):
            pending += list(node.value)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    return out


def markdown_problems(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    front_end = (
        next((i for i, line in enumerate(lines[1:], 1) if line == "---"), 0) if lines[:1] == ["---"] else 0
    )
    front = set(range(1, front_end + 2)) if front_end > 0 else set[int]()
    # Frontmatter is not Markdown: blanked for the parser, read line by line below.
    code = markdown_code("\n".join("" if n in front else line for n, line in enumerate(lines, 1)))
    found = code_problems(code)
    # MCP references in running text, one normalized block at a time (`reference_text`), soft
    # line breaks read as spaces: a reference wrapped across lines is still one (`check_text_at`).
    for start, paragraph in code.prose:
        for pos, why in check_text_at(paragraph):
            found.setdefault(start + paragraph.count("\n", 0, pos), []).append(why)
    if front_end > 0:
        for n, why in frontmatter_problems("\n".join(lines[1:front_end]), first_line=2):
            # A value's offset is counted in its decoded text (`"a\nb"` is one source line), so it
            # can run past the frontmatter: it is reported at the closing `---`, never dropped.
            found.setdefault(min(n, front_end + 1), []).append(why)
    # Every finding is reported: one whose line is outside the file lands on its nearest line.
    last = max(len(lines), 1)
    for n in [n for n in found if not 1 <= n <= last]:
        found.setdefault(min(max(n, 1), last), []).extend(found.pop(n))
    rel = _rel(path)
    out: list[str] = []
    for n, line in enumerate(lines, 1):
        problems = list(found.get(n, []))
        if n in front:  # frontmatter: its decoded values are judged above; paths, line by line
            problems += path_problems(line, path, literal=True)
        elif n in code.body:  # a code block's line, read literally
            body = code.body[n]
            problems += path_problems(body, path, literal=True)  # its MCP names: `code_problems`
        elif n not in code.lines:
            problems += path_problems(line, path)
            for target in WIKILINK.findall(SPAN.sub("", line)):
                if (why := check_wikilink(target, path)) is not None:
                    problems.append(f"[[{target}]]: {why}")
        out += [f"{rel}:{n} {p}" for p in dict.fromkeys(problems)]
    return out


def check_wikilink(target: str, where: Path) -> str | None:
    """A `[[note#Heading|alias]]` resolves the way Obsidian resolves it: a vault path, or a unique
    note name; and its heading is still a heading there (case-insensitive, as Obsidian matches).
    Moving or renaming either breaks every link to it, which is why AGENTS.md says "no Obsidian
    moves"."""
    note, _, heading = target.replace("\\|", "|").split("|", 1)[0].partition("#")  # `\|` inside a table
    if PLACEHOLDER.search(note) or _private(note):
        return None
    if not note:
        file = where
    else:
        candidates = [ROOT / note, ROOT / f"{note}.md", where.parent / f"{note}.md"]
        file = next((c for c in candidates if c.is_file()), None)
        if file is None:
            file = next((p for p in markdown_files() if p.stem == Path(note).name), None)
        if file is None:
            return "no such note"
    if heading and not heading.startswith("^") and file.suffix == ".md":
        text = file.read_text(encoding="utf-8")
        headings = {h.strip().casefold() for h in re.findall(r"^#+ (.+)$", text, re.M)}
        if heading.strip().casefold() not in headings:
            return f"no heading {heading!r} in {_rel(file)}"
    return None


def python_problems(path: Path) -> list[str]:
    """Commands, hook names and MCP tools in every string constant; paths and `module.name` in
    every docstring: the module's, and each function's and class's."""
    rel = _rel(path)
    out: list[str] = []
    for lineno, value in strings_of(path):
        problems = check_text(value)
        problems += [why for whys in code_problems(source_code(value)).values() for why in whys]
        out += [f"{rel}:{lineno} {p}" for p in problems]
    for lineno, doc in docstrings_of(path):
        where = f"{rel}:{lineno} docstring"
        out += [f"{where} {why}" for why in path_problems(doc, path)]
        spans = source_code(doc).spans
        out += [f"{where} {why}" for _n, span in spans if (why := check_symbol(span)) is not None]
    return out


def source_code(text: str) -> MarkdownCode:
    """The code in a string constant, a docstring or a tool description: the same extraction as
    a Markdown file's (`markdown_code`), on the text as a reader gets it. Only the indentation
    the Python source added is removed, by `_walked`; a tool description is read as the client
    gets it. Text that cannot hold code (one line, no backtick or tilde) is not parsed."""
    if "`" not in text and "~" not in text and "\n" not in text:
        return MarkdownCode()
    return _source_code(text)


@functools.cache
def _source_code(text: str) -> MarkdownCode:
    return markdown_code(text)


def code_problems(code: MarkdownCode) -> dict[int, list[str]]:
    """The checks every source's code gets, by line: each shell block's commands, read whole
    (`command_problems`), each code block line's hook names and MCP tool and action names
    (`check_text`), and each span's commands and hook
    names (`check_span`). Markdown files, docstrings, string constants and tool descriptions
    all come here through `markdown_code`: one extraction, one set of checks."""
    found: dict[int, list[str]] = {}
    for start, info, body in code.blocks:
        if info.lower() in SHELL_FENCES:  # read whole: continuations and quotes span lines
            for k, why in command_problems("\n".join(body)):
                found.setdefault(start + k, []).append(why)
        for k, line in enumerate(body):
            if (why := check_hook_name(line)) is not None:
                found.setdefault(start + k, []).append(why)
            # MCP tool and action names, a code line at a time: one rule for every source.
            found.setdefault(start + k, []).extend(code_line_text(line))
    for n, span in code.spans:
        found.setdefault(n, []).extend(check_span(span))
    return found


def description_problems(descriptions: dict[str, str]) -> list[str]:
    problems: list[str] = []
    for tool, description in sorted(descriptions.items()):
        found = check_text(description) + path_problems(description, SRC)
        found += [why for whys in code_problems(source_code(description)).values() for why in whys]
        problems += [f"{tool}: {p}" for p in found]
    return problems


def hook_config_problems(path: Path) -> list[str]:
    """Every `agent ...` a client's hook config runs (`agent hook <name>`, `agent status line`)."""
    commands: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():  # pyright: ignore[reportUnknownVariableType]
                if key == "command" and isinstance(value, str):
                    commands.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for value in node:  # pyright: ignore[reportUnknownVariableType]
                walk(value)

    walk(json.loads(path.read_text(encoding="utf-8")))
    problems: list[str] = []
    for command in commands:
        # The launcher is wrapped (`node -e "<script>" agent hook stop`): the script is the
        # wrapper's, and what follows it is the example.
        wrapped = _HOOK_WRAPPER.fullmatch(command)
        example = wrapped.group(1) if wrapped else command
        if not any(line.argvs or line.unverifiable for line in example_lines(example)):
            problems.append(f"{_rel(path)}: no `agent` command in {command[-80:]!r}")
        problems += [f"{_rel(path)}: {why}" for _line, why in command_problems(example)]
    return problems


#: The wrapper a client's hook config runs the launcher through: `node -e "<script>" agent ...`.
_HOOK_WRAPPER = re.compile(r'node -e "(?:[^"\\]|\\.)*" (agent(?: .*)?)')


def _allowed(problem: str) -> bool:
    for (file, ref), _reason in ALLOWED.items():
        if problem.startswith(file + ":") and (ref == "*" or f"`{ref}`" in problem):
            return True
    return False


# ---------------------------------------------------------------------- the tests


def test_manual_chapters_link_to_the_next_chapter():
    chapters = sorted((ROOT / "docs").glob("[0-9][0-9]-*.md"))
    problems: list[str] = []
    for chapter, following in zip(chapters[:-1], chapters[1:], strict=True):
        links = [
            str(child.attrGet("href") or "")
            for token in _markdown().parse(chapter.read_text(encoding="utf-8"))
            for child in token.children or []
            if child.type == "link_open"
        ]
        target = links[-1].split("#", 1)[0] if links else ""
        if not target or (chapter.parent / target).resolve() != following.resolve():
            problems.append(f"{_rel(chapter)}: last link must point to {following.name}")
    assert problems == []


def test_markdown_cites_only_what_exists():
    problems = [p for path in markdown_files() for p in markdown_problems(path) if not _allowed(p)]
    assert problems == []


def test_python_strings_and_docstrings_cite_only_what_exists():
    problems = [p for path in sorted(SRC.rglob("*.py")) for p in python_problems(path) if not _allowed(p)]
    assert problems == []


def test_tool_descriptions_cite_only_what_exists():
    assert description_problems({tool: d for tool, (d, _) in mcp_tools().items()}) == []


@pytest.mark.parametrize("config", HOOK_CONFIGS)
def test_wired_hook_commands_exist(config: str):
    assert hook_config_problems(ROOT / config) == []


@pytest.mark.parametrize("tool", sorted(name for name, (_d, actions) in mcp_tools().items() if actions))
def test_every_action_is_named_in_its_tool_description(tool: str):
    """A model chooses an action from the description; one it never names is one it never picks
    (`collect-files` was in the enum and nowhere in the text)."""
    description, actions = mcp_tools()[tool]
    assert sorted(a for a in actions if f"`{a}`" not in description) == []


# ---------------------------------------------------------------------- the checker itself


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("dispatch show 001-mac-01", None),
        ("--json status check", None),
        ("dispatch new agent --feature 9 --dry-run", None),
        ('log current "Avoid --squash in this repo"', None),
        ("status check 2>&1 | head", None),
        ("dispatch nope", "invalid choice: 'nope'"),
        ("status render --nope", "unrecognized arguments: --nope"),
        ("status check -Z", "unrecognized arguments: -Z"),
        ("status check --json", "unrecognized arguments: --json"),  # a global flag after the command
        # Hook arguments, `--`, typed and choice values, placeholders.
        ("hook stop -Z", "unrecognized arguments: -Z"),
        ("hook stop --probe", None),
        ('log current -- "--squash"', None),
        ("status --help", None),
        ("dispatch collect --wait not_a_number", "argument --wait: invalid float value: 'not_a_number'"),
        ("dispatch collect --wait <seconds>", None),
        (
            'dispatch new agent --feature "<id>" --provider nonexistent',
            "argument --provider: invalid choice: 'nonexistent'",
        ),
        ("dispatch new agent --provider <provider>", None),
        ("setup --notes <path>", None),
        ("setup --notes", "argument --notes: expected one argument"),
        # Argparse's prefix abbreviations, and `a|b` alternatives.
        ("--ver", None),
        ("dispatch new agent --provider=codex --feature=1 --dry", None),
        ("dispatch collect --wait=1 --fea=1", "unrecognized arguments: --fea=1"),
        ("procs list|keep|never_exists", "invalid choice: 'never_exists'"),
        ("procs list|keep", None),
        ("mcp --profile manager|worker", None),
        (
            "mcp --profile manager|nope",
            "argument --profile: invalid choice: 'nope'",
        ),
        # Alternatives in every position expand to whole commands.
        ("dispatch collect --wait 1|not_a_number", "argument --wait: invalid float value: 'not_a_number'"),
        ("dispatch collect --wait not_a_number|1", "argument --wait: invalid float value: 'not_a_number'"),
        ("hook stop|ask-guard", None),
        ("hook stop|no-such-hook", "invalid choice: 'no-such-hook'"),
        # Alternatives that begin with punctuation reach argparse, and an
        # example past the expansion cap fails instead of passing on the checked part.
        ("dispatch collect --wait 1|.bad", "argument --wait: invalid float value: '.bad'"),
        ("dispatch collect --wait 1|.5", None),
        ("dispatch collect --wait 1|-bad", "argument --wait: expected one argument"),
        (
            "dispatch collect --wait " + "|".join([*(str(n) for n in range(1, 65)), "not_a_number"]),
            "65 alternative combinations exceed the 64 this lint checks; split the example",
        ),
        ("status check || true", None),
        # Code is literal (no punctuation stripped), and a digit is a file
        # descriptor only when attached to its redirect.
        ("dispatch collect --wait 1|2:", "argument --wait: invalid float value: '2:'"),
        ("dispatch collect --wait 2:", "argument --wait: invalid float value: '2:'"),
        ("dispatch collect --wait 1 > /dev/null", None),
        ("dispatch collect --wait 1 2>/dev/null", None),
        ("dispatch collect --wait 1 | cat", None),
        ("dispatch collect --wait 1 || cat", None),
        # Quoted text is argument data, never an operator, comment, placeholder
        # or alternatives; a placeholder is a recognised form in an unquoted word.
        ('dispatch collect --wait "1>2"', "argument --wait: invalid float value: '1>2'"),
        ("dispatch collect --wait '<x>'", "argument --wait: invalid float value: '<x>'"),
        ('dispatch collect --wait "1|x"', "argument --wait: invalid float value: '1|x'"),
        ('dispatch new agent --prompt "# Heading"', None),
        ("dispatch new agent --prompt x # a comment", None),
        ("dispatch show 017-<tag>-01", None),
        ("mcp --profile <manager|worker>", None),
        ("hook ask-guard", None),
        ("hook done-unticked", None),
        ("hook not-a-hook", "invalid choice: 'not-a-hook'"),
        ("dispatch <sub> --whatever", None),
        # The real parser is the only judge; a missing required argument is
        # filled, and what is left is still judged.
        ("export /tmp/review-output --json", "unrecognized arguments: --json"),
        ("export /tmp/review-output -m example --json", "unrecognized arguments: --json"),
        ("export /tmp/review-output -mexample", None),
        ("export /tmp/review-output -m example", None),
        ("export /tmp/review-output", None),  # incomplete, and otherwise right
        ("--json export /tmp/review-output", None),
        ("dispatch new agent --feature=<id>", None),
        ('status check ""', "unrecognized arguments: "),
    ],
)
def test_check_command(text: str, why: str | None):
    found = check_command(text)
    assert found == why if why is None else (found is not None and why in found), found


@pytest.mark.parametrize(
    ("content", "found"),
    [
        ("`uv run agent never_exists`", "invalid choice: 'never_exists'"),
        ("`bin/agent never_exists`", "invalid choice: 'never_exists'"),
        ("`agent status check -Z`", "unrecognized arguments: -Z"),
        ("Use the `never_exists` tool.", "no MCP tool"),
        ("Call the `harness` tool with action `never-exists`.", "has no action `never-exists`"),
        ("The harness tool has action never-exists.", "has no action `never-exists`"),
        ("Run the `harness` tool's `never-exists` action.", "has no action `never-exists`"),
        (
            "---\nname: probe\ndescription: Read harness/never-exists.md first.\n---",
            "harness/never-exists.md",
        ),
        ("```text\nharness/never-exists.md\n```", "harness/never-exists.md"),
        ("```sh\nbin/agent never_exists\n```", "invalid choice: 'never_exists'"),
        ("See harness/never-exists.md for more.", "harness/never-exists.md"),
        ("`notes-sync-never-exists`", "no hook or group"),
        ("`nope-guard`", "no hook or group"),
        ("`agent --json status check`", None),
        ('`agent log current "Avoid --squash in this repo"`', None),
        ("[[harness/maintenance#instructions and clients]]", None),
        ("[[harness/maintenance#No such heading]]", "no heading"),
        ("Call the `harness` tool with action `doctor`.", None),
        ("`protect-generated`", None),
        # Fences, unquoted forms, two commands in one span.
        (
            "```text\nCall the `harness` tool with action `never-exists`.\n```\n",
            "has no action `never-exists`",
        ),
        ("```text\nCall the `never_exists` tool.\n```\n", "no MCP tool"),
        ("Call the never_exists tool.", "no MCP tool"),
        ("The harness tool has action neverexists.", "has no action `neverexists`"),
        ("Use action neverexists of the harness tool.", "has no action `neverexists`"),
        ("The harness tool has action doctor.", None),
        ("Pick the same tool for the job.", None),
        ("`agent status check ; agent never_exists`", "invalid choice: 'never_exists'"),
        # Every expansion of `a|b` is checked through its remaining arguments,
        # and literal contexts keep relative and bare-file path checks.
        ("Run `agent status check|render -Z`.", "unrecognized arguments: -Z"),
        ("Run `agent status check|render --never-exists`.", "unrecognized arguments: --never-exists"),
        ("Run `agent status check|render`.", None),
        (
            "---\ndescription: Read `doctor/checks_vault.py` first.\n---\n",
            "doctor/checks_vault.py",
        ),
        ("---\nfile: never_exists.py\n---\n", "never_exists.py"),
        ("```text\nnever_exists.py\n```\n", "never_exists.py"),
        ("```text\nRead `never_exists.py`.\n```\n", "never_exists.py"),
        ("Read `never_exists.py`.\n", "never_exists.py"),
        ("```text\nsrc/agent_cli/status.py and status.py\n```\n", None),
        # A span or fence is tokenized whole before it is split into commands,
        # continuations join lines, escapes are honoured, and the rest fails closed.
        (
            '`agent dispatch new agent --prompt "a;b" --no-such-flag`',
            "unrecognized arguments: --no-such-flag",
        ),
        (
            '`agent dispatch new agent --prompt "a | b" --no-such-flag`',
            "unrecognized arguments: --no-such-flag",
        ),
        (
            '`agent dispatch new agent --prompt "Say \\"hello\\"" --no-such-flag`',
            "unrecognized arguments: --no-such-flag",
        ),
        ('`agent dispatch new agent --prompt "Say \\"hello\\""`', None),
        ("```sh\nagent dispatch collect --wait \\\n1\n```\n", None),
        ('```sh\nagent dispatch collect --wait "1\\\n.5"\n```\n', CANNOT_VERIFY),  # inside a word
        ("```sh\nagent dispatch collect \\\n  --nope 1\n```\n", "unrecognized arguments: --nope"),
        ('`agent dispatch collect --wait "1`', CANNOT_VERIFY),
        ("`agent dispatch collect --wait '1`", CANNOT_VERIFY),
        ("`agent dispatch collect --wait $'1'`", CANNOT_VERIFY),
        ("```sh\nagent status check $(date)\n```\n", CANNOT_VERIFY),
        ("```sh\nagent status check <<EOF\nx\nEOF\n```\n", CANNOT_VERIFY),
        ("```text\nthe agent can do that\n```\n", None),
        ("```sh\n$ agent status check\n```\n", None),
        # Any `$` fails closed; so does malformed structure.
        ('```sh\nLABEL="two words" agent status check --nope\n```\n', "unrecognized arguments: --nope"),
        ('```sh\nagent dispatch new agent --prompt "$@"\n```\n', CANNOT_VERIFY),
        ('```sh\nagent dispatch new agent --prompt "$?"\n```\n', CANNOT_VERIFY),
        ('```sh\nagent dispatch new agent --prompt "$$"\n```\n', CANNOT_VERIFY),
        ("```sh\nagent status check >\n```\n", CANNOT_VERIFY),
        ("```sh\nagent status check ;;\n```\n", CANNOT_VERIFY),
        ("```sh\n| agent status check\n```\n", CANNOT_VERIFY),
        ("```sh\nagent status check |\n```\n", CANNOT_VERIFY),
        # A strict whitelist grammar. Every line of a span or shell fence that
        # names the launcher must parse in it; a bare launcher only; nothing else is guessed.
        ("```sh\n'agent' status check --nope\n```\n", CANNOT_VERIFY),
        ("```sh\nag\\\nent status check --nope\n```\n", CANNOT_VERIFY),
        ("```sh\na\\gent status check --nope\n```\n", CANNOT_VERIFY),
        ('```sh\nagent dispatch new agent --prompt "\\$@"\n```\n', CANNOT_VERIFY),
        ("```sh\nagent status check &\n```\n", CANNOT_VERIFY),
        ("```sh\n! agent status check\n```\n", CANNOT_VERIFY),
        ("```sh\nexec agent status check\n```\n", CANNOT_VERIFY),
        ("```sh\nif true; then agent status check; fi\n```\n", CANNOT_VERIFY),
        ("```sh\nwhich agent\n```\n", CANNOT_VERIFY),
        ("```sh\nagent status check # a comment \\\nagent never_exists\n```\n", CANNOT_VERIFY),
        ("```sh\n# agent never_exists\nagent status check\n```\n", None),  # a comment is not run
        (
            "```\nagent status check --nope\n```\n",
            "unrecognized arguments: --nope",
        ),  # an unlabelled fence is shell
        ("```console\n$ agent status check --nope\n```\n", "unrecognized arguments: --nope"),
        ("```text\nagent status check --nope\n```\n", None),  # a `text` fence is prose
        ("```text\nthe agent can't do that\n```\n", None),
        ("`agent '' status check`", "invalid choice: ''"),
        ('`agent status check ""`', "unrecognized arguments"),
        ("`the agent`", CANNOT_VERIFY),
        ("`verify:agent`, `agent/016`, `<agent>.meta.json`", None),  # not the launcher
        # Quotes across lines, launchers by path, fences found by CommonMark.
        ("```sh\nprintf '\n#' ; agent status check --nope\n```\n", CANNOT_VERIFY),
        ("```sh\nprintf '\nagent status check\n'\n```\n", CANNOT_VERIFY),
        ('```sh\nprintf "\n#" ; agent status check --nope\n```\n', CANNOT_VERIFY),
        ("```sh\nagent status check\nprintf 'it''s'\n```\n", None),  # quotes closed on each line
        ("```sh\n../bin/agent status check --nope\n```\n", "unrecognized arguments: --nope"),
        ("```sh\n/abs/bin/agent status check --nope\n```\n", "unrecognized arguments: --nope"),
        ("```sh\n'/abs/bin/agent' status check --nope\n```\n", CANNOT_VERIFY),
        ("`../bin/agent status check --nope`", "unrecognized arguments: --nope"),
        ("`Edit(//<parent>/*/bin/agent)`", None),  # a permission rule, not a command
        ("````sh\n```text\nagent status check --nope\n```\n````\n", "unrecognized arguments: --nope"),
        ("- Example:\n\n  ```sh\n  agent status check --nope\n  ```\n", "unrecognized arguments: --nope"),
        ("> ```sh\n> agent status check --nope\n> ```\n", "unrecognized arguments: --nope"),
        ("Run ``agent status check --nope``.\n", "unrecognized arguments: --nope"),
        ("Run `agent status\ncheck --nope` here.\n", "unrecognized arguments: --nope"),  # a span across lines
        ("    agent status check --nope\n", "unrecognized arguments: --nope"),  # an indented code block
        ("<!-- `agent status check --nope` -->\n", "unrecognized arguments: --nope"),  # an HTML block
        ("| a | b |\n|---|---|\n| `agent status check --nope` | x |\n", "unrecognized arguments: --nope"),
        # Launcher paths by their last component; a whitelist of command names.
        ("```sh\n./dir:name/agent status check --nope\n```\n", CANNOT_VERIFY),
        ("`./dir:name/agent status check --nope`", CANNOT_VERIFY),
        ("```sh\nC:/bin/agent status check --nope\n```\n", CANNOT_VERIFY),
        ("`C:/bin/agent status check --nope`", CANNOT_VERIFY),
        ("```sh\n/abs/dir:name/agent status check --nope\n```\n", CANNOT_VERIFY),
        ("`C:\\bin\\agent.cmd status check`", CANNOT_VERIFY),
        ("```sh\n./ordinary/agent status check --nope\n```\n", "unrecognized arguments: --nope"),
        ("```sh\nif true ; agent status check\n```\n", CANNOT_VERIFY),
        ("`if true ; agent status check`", CANNOT_VERIFY),
        ("```sh\nif true ; then true ; fi ; agent status check\n```\n", CANNOT_VERIFY),
        ("```sh\nPATH=bin agent status check\n```\n", CANNOT_VERIFY),
        ("```sh\nfalse && agent status check\n```\n", CANNOT_VERIFY),
        ("```sh\nagent status check | time cat\n```\n", CANNOT_VERIFY),
        ("```sh\nagent status check | grep x || true\n```\n", None),
        # argparse reads an option-like word holding a space as a positional (`id` here), so the
        # real parser accepts this one; the lint agrees with it rather than second-guessing.
        ('```sh\nagent dispatch collect --prompt="hello world"\n```\n', None),
        ("```sh\nagent dispatch collect x --prompt=hello\n```\n", "unrecognized arguments: --prompt=hello"),
        ("```sh\nagent export /tmp/review-output --json\n```\n", "unrecognized arguments: --json"),
        ("```sh\nagent export /tmp/review-output -mexample\n```\n", None),
        # A missing subcommand does not hide the words present.
        ("```sh\nagent status --nope\n```\n", "unrecognized arguments: --nope"),
        ("`agent dispatch --nope`", "unrecognized arguments: --nope"),
        ("`agent feature --nope`", "unrecognized arguments: --nope"),
        ("`agent --nope`", "unrecognized arguments: --nope"),
        ("`agent status`", None),  # incomplete, and otherwise right
        ("`agent dispatch <sub>`", None),
        ("`agent --nope dispatch <sub>`", "unrecognized arguments: --nope"),
        ("Summary.\n\n    agent status check --nope\n", "unrecognized arguments: --nope"),
        # Frontmatter is read as YAML, every string value, block scalars too.
        (
            "---\ndescription: >-\n  Use `agent status check --nope` to verify.\n---\n",
            "unrecognized arguments: --nope",
        ),
        (
            "---\ndescription: |\n  First line.\n  Use `agent status check --nope` to verify.\n---\n",
            "unrecognized arguments: --nope",
        ),
        (
            "---\ndescription: Use this\n  `agent status check --nope` to verify.\n---\n",
            "unrecognized arguments: --nope",
        ),
        (
            "---\nmeta:\n  examples:\n    - Run `agent status check --nope`.\n---\n",
            "unrecognized arguments: --nope",
        ),
        ("---\nname: probe\nhook: nope-guard\n---\n", "no hook or group"),
        ("---\ndescription: >-\n  Use `agent status check` to verify.\n---\n", None),
        ("---\ndescription: [unclosed\n---\n", "not valid YAML"),
        # An escaped `\n` puts a finding past the file's last line; it is
        # reported at the closing `---`, never dropped.
        ('---\ndescription: "Intro.\\n\\nUse `agent status check --nope`."\n---\nBody.\n', "--nope"),
        ('---\nname: probe\ndescription: "Intro.\\n\\n\\nUse `agent status check --nope`."\n---\n', "--nope"),
        ('---\ndescription: "Intro.\\n```sh\\nagent status check --nope\\n```"\n---\nBody.\n', "--nope"),
        ('---\ndescription: "Intro.\\n\\n    agent status check --nope\\n"\n---\nBody.\n', "--nope"),
        # The module launcher (the Windows route) is a launcher; folded
        # frontmatter keeps its MCP references.
        ("`python -m agent_cli status check --nope`", "unrecognized arguments: --nope"),
        ("```sh\npython.exe -I -m agent_cli status check --nope\n```\n", "unrecognized arguments: --nope"),
        ("```sh\nuv run python3 -m agent_cli status check --nope\n```\n", "unrecognized arguments: --nope"),
        ("`.venv/bin/python -I -m agent_cli --nope`", "unrecognized arguments: --nope"),
        ("`python.exe -I -m agent_cli`", None),  # incomplete, and otherwise right
        ("`python -m agent_cli status check`", None),
        ("`python -m agent_cli.launcher --output x`", None),  # another module, not the CLI
        ('`"python.exe" -m agent_cli status check`', CANNOT_VERIFY),
        ("`python -magent_cli status check`", CANNOT_VERIFY),
        ('`python -m "agent_cli" status check`', CANNOT_VERIFY),
        ("`C:\\py\\python.exe -I -m agent_cli status check`", CANNOT_VERIFY),
        (
            "---\ndescription: >-\n  The `harness` tool has action\n  neverexists.\n---\n",
            "has no action `neverexists`",
        ),
        (
            "---\ndescription: The `harness` tool has action neverexists.\n---\n",
            "has no action `neverexists`",
        ),
        ("---\ndescription: >-\n  Call the `never_exists`\n  tool.\n---\n", "no MCP tool"),
        ("---\ndescription: Call the `never_exists` tool.\n---\n", "no MCP tool"),
        ("---\ndescription: >-\n  The `harness` tool has action\n  doctor.\n---\n", None),
        # MCP references are read a paragraph at a time, soft breaks as spaces.
        ("Call the `never_exists`\ntool.\n", "`never_exists`: no MCP tool"),
        ("Call the `never_exists` tool.\n", "`never_exists`: no MCP tool"),
        ("The `dispatch_read` tool has action\n`land`.\n", "`dispatch_read` has no action `land`"),
        ("The `dispatch_read` tool has action `land`.\n", "`dispatch_read` has no action `land`"),
        ("The harness tool has action\nneverexists.\n", "has no action `neverexists`"),
        (
            "- Then run the `harness`\n  tool's `never_exists_x` action, and send the path.\n",
            "no action `never_exists_x`",
        ),
        ("> Call the `never_exists`\n> tool.\n", "no MCP tool"),
        ("| a |\n|---|\n| the `never_exists` tool |\n", "no MCP tool"),
        ("- Then run the `harness`\n  tool's `notes_publish` action.\n", None),
        ("Call the `never_exists`\n\ntool.\n", None),  # two paragraphs: no reference
        (
            "---\ndescription: |-\n  The harness tool has action\n  neverexists.\n---\n",
            "has no action `neverexists`",
        ),
        # One rule for a tool reference, whatever the phrasing.
        ("The never_exists tool is read-only.\n", "`never_exists`: no MCP tool"),
        ("Use the never_exists tool to read a result.\n", "`never_exists`: no MCP tool"),
        ("The never_exists\ntool is read-only.\n", "`never_exists`: no MCP tool"),
        ("Use the [`never_exists`](harness/sessions.md) tool.\n", "`never_exists`: no MCP tool"),
        ("Use the **never_exists** tool.\n", "`never_exists`: no MCP tool"),
        ("Use the ``never_exists`` tool.\n", "`never_exists`: no MCP tool"),
        ("Use the never_exists MCP tool.\n", "`never_exists`: no MCP tool"),
        ("Use the `never_exists` and `never_either` tools.\n", "`never_either`: no MCP tool"),
        (
            '---\ndescription: "Preview it with the never_exists MCP tool."\n---\n',
            "`never_exists`: no MCP tool",
        ),
        ('---\ndescription: "Preview it with the `nonesuch` MCP tool."\n---\n', "`nonesuch`: no MCP tool"),
        ('---\ndescription: "Preview it with the `dispatch` MCP tool."\n---\n', None),
        ("The `Nope` tool.\n", "`Nope`: no MCP tool"),
        ("The `Bash` MCP tool.\n", "`Bash`: no MCP tool"),
        ("Use the right tool, the `Bash` tool, the Read tool or an MCP tool.\n", None),
        ("Use the agent MCP tools, the read-only MCP tool and the MCP tool.\n", None),
        # A tool name is judged when written as code (or snake_case); a bare
        # English word is prose. Title case, emphasis, titled links and lists are all read.
        ("# The `never_exists` MCP Tool\n", "`never_exists`: no MCP tool"),
        ("# The `never_exists` MCP Tools\n", "`never_exists`: no MCP tool"),
        ("Use the `never_exists` MCP TOOL.\n", "`never_exists`: no MCP tool"),
        ("Use the **`never_exists`** tool.\n", "`never_exists`: no MCP tool"),
        ("Use the *`never_exists`* tool.\n", "`never_exists`: no MCP tool"),
        ('Use the [`never_exists`](harness/sessions.md "Tools") tool.\n', "`never_exists`: no MCP tool"),
        ("Use the `never_exists` and `dispatch` tools.\n", "`never_exists`: no MCP tool"),
        ("Use the `never_exists`, `dispatch` or `harness` tools.\n", "`never_exists`: no MCP tool"),
        ("Use the `dispatch`/`never_exists` tools.\n", "`never_exists`: no MCP tool"),
        ("Use the `dispatch`, `harness` and `lease` tools.\n", None),
        ("Choose the appropriate MCP tool for the task.\n", None),
        ("Use the right MCP tool for each operation.\n", None),
        ("Call the selected MCP tool after approval.\n", None),
        ("Use the `never_exists`.\n\nTools follow.\n", None),
        ("Use `never_exists` then the tool.\n", None),  # not directly before "tool"
        (
            '---\ndescription: "Choose the appropriate MCP tool, preview it with the `dispatch` MCP tool."\n---\n',
            None,
        ),
        # What follows a name is read whole, so wrapping or emphasis inside
        # "MCP tool" does not hide it.
        ("> Call the `never_exists` MCP\n> tool.\n", "`never_exists`: no MCP tool"),
        ("- Call the `never_exists` and `dispatch` MCP\n  tools.\n", "`never_exists`: no MCP tool"),
        ("Call the `never_exists` **MCP** tool.\n", "`never_exists`: no MCP tool"),
        ("Call the `never_exists` MCP **tool**.\n", "`never_exists`: no MCP tool"),
        ("Call the `never_exists` *MCP\ntool*.\n", "`never_exists`: no MCP tool"),
        (
            "---\ndescription: |-\n  Preview it with the `never_exists` MCP\n  tool.\n---\n",
            "`never_exists`: no MCP tool",
        ),
        (
            "---\ndescription: >-\n  Preview it with the `never_exists` **MCP**\n  tool.\n---\n",
            "`never_exists`: no MCP tool",
        ),
        ("Call the `dispatch` MCP\ntool.\n", None),
        ("Call the `dispatch` **MCP** tool, then the `harness`\n*tool*.\n", None),
    ],
)
def test_markdown_probe(tmp_path: Path, content: str, found: str | None):
    """Each reference form the lint promises is checked."""
    doc = tmp_path / "probe.md"
    doc.write_text(content, encoding="utf-8")
    problems = markdown_problems(doc)
    if found is None:
        assert problems == []
    else:
        assert any(found in p for p in problems), problems


#: Shell examples run through `/bin/sh` with a stand-in `agent` that records its argv. Fixed
#: here; doc text is never run. `None` marks a row the lint must refuse as not checkable.
DIFFERENTIAL = [
    # Codex r9
    ('dispatch new agent --prompt "Say \\"hello\\""', True),
    ('dispatch new agent --prompt "Say \\"a|b\\""', True),
    ('dispatch new agent --prompt "Say \\"hello\\"" --no-such-flag', True),
    ('dispatch collect --wait "1\\"2"', True),
    ('dispatch collect --wait ""', True),
    ("dispatch collect --wait 1\\\n.5", None),  # a continuation only after whitespace
    ("dispatch collect --wait \\\n 1", True),
    ("dispatch collect --wa\\\nit 1", None),
    ('dispatch collect --wait "1\\\n.5"', None),
    ("dispatch new agent --prompt 'Say \"hello\"'", True),
    ('dispatch new agent --prompt "a;b"', True),
    ('dispatch new agent --prompt "a | b"', True),
    ('dispatch new agent --prompt "a;b" --no-such-flag', True),
    ('dispatch collect --wait "1', None),
    ("dispatch collect --wait '1", None),
    ("dispatch collect --wait $'1'", None),
    # Codex r8
    ('dispatch collect --wait "1>2"', True),
    ('dispatch new agent --prompt "# Heading"', True),
    ("dispatch collect --wait '<x>'", True),
    ('dispatch collect --wait "1|x"', True),
    ("dispatch new agent --prompt x # a comment", True),
    # earlier controls
    ("status check 2>&1 | cat", True),
    ("dispatch collect --wait 1 > /dev/null", True),
    ("dispatch collect --wait 1 2>/dev/null", True),
    ("dispatch collect --wait 1 || true", True),
    ("status check ; true", True),
    ("status check; true", None),  # a separator needs a space on both sides
    ('log current "Avoid --squash in this repo"', True),
    ("dispatch collect --wait 2:", True),
    ('log current -- "--squash"', True),
    ("--json status check", True),
    # outside the dialect
    ("$(echo status) check", None),
    ("`echo status` check", None),
    ("status check ~/x", None),
    ('dispatch new agent --prompt "$HOME"', None),
]
#: Whole commands, for rows where the launcher itself is quoted, continued or preceded.
DIFFERENTIAL_FULL = [
    # Codex r10
    ('agent dispatch new agent --prompt "$@"', None),
    ('agent dispatch new agent --prompt "$?"', None),
    ('agent dispatch new agent --prompt "$$"', None),
    ("'agent' status check --nope", None),  # the launcher must be bare
    ("ag\\\nent status check --nope", None),
    ('LABEL="two words" agent status check --nope', True),
    ("agent status check >", None),
    ("agent status check ;;", None),
    ('agent dispatch new agent --prompt "\\$@"', None),  # `\\"` and `\\\\` are the only escapes
    ('agent dispatch new agent --prompt "Say \\"hello\\"" --no-such-flag', True),
    ("| agent status check", None),
    ("agent status check |", None),
    ("A=1 B='x y' agent status check", True),
    # Codex r11
    ("agent status check", True),
    ("agent status check --nope", True),
    ("LA\\\nBEL=x agent status check --nope", None),
    ("LABEL\\\n=x agent status check --nope", None),
    ("\\\nLABEL=x agent status check --nope", None),
    ("LABEL=\\\nx agent status check --nope", None),
    ('agent dispatch collect --wait ""', True),
    ("agent '' status check", True),
    ('agent status check ""', True),
    ('agent dispatch new "" --prompt x', True),
    ("agent dispatch collect --wait 1>&1.5", None),
    ("agent dispatch collect --wait 1 >&1.5", None),
    ("agent dispatch collect --wait 1>&0'1'", None),
    ("agent dispatch collect --wait 1>&0\\1", None),
    ("! agent status check --nope", None),
    ("command agent status check --nope", None),
    ("exec agent status check --nope", None),
    ("if agent status check --nope; then :; fi", None),
    ("if true; then agent status check --nope; fi", None),
    ("LABEL=~ agent status check --nope", None),
    ('agent dispatch new agent --prompt "x|y"', True),
    ("a\\gent status check --nope", None),
    ("agent dispatch collect --wait '1'>/dev/null", None),
    ("agent dispatch new agent --prompt '$@'", True),
    ("agent status check 2>&1 | cat", True),
    ("agent dispatch collect --wait 1 >/dev/null", True),
    ("agent dispatch collect --wait --prompt='a b' > out.txt", True),
    ("agent status check # a comment \\\nagent status check --nope", None),
    ("agent status check \\\n# a comment\nagent status check --nope", True),
    # A quote left open across lines; launchers given by path
    ("printf '\n#' ; agent status check --nope", None),
    ("printf '\nagent status check\n'", None),  # the shell runs no CLI here: never accepted
    ('printf "\n#" ; agent status check --nope', None),
    ("../bin/agent status check --nope", True),
    ("{shim} status check --nope", True),
    ("'{shim}' status check --nope", None),
    ('"../bin/agent" status check', None),
    ("agent dispatch collect --wait 1 > /dev/null", True),
    ("agent '' status check", True),
    # Launcher paths outside `[A-Za-z0-9_./-]`; names in command position
    ("./dir:name/agent status check --nope", None),
    ("C:/bin/agent status check --nope", None),
    ("/abs/dir:name/agent status check --nope", None),
    ("C:\\bin\\agent.cmd status check --nope", None),
    ("./ordinary/agent status check --nope", True),
    ("if true ; agent status check", None),
    ("if true ; then true ; fi ; agent status check", None),
    ("PATH=bin agent status check", None),
    ("false && agent status check", None),
    ("true ; agent status check", None),
    ("agent status check && agent status check --nope", None),
    ("agent status check | exec cat", None),
    ("agent status check | time cat", None),
    ("agent status check ; agent status check --nope", True),
    ("agent status check | grep x || true", True),
    ('agent dispatch collect --prompt="hello world"', True),
    # Codex r14
    ("agent export /tmp/review-output --json", True),
    ("agent export /tmp/review-output -mexample", True),
    ("LABEL= agent status check", True),
    ("LABEL='a b' agent status check", True),
    ('LABEL="a b" agent status check', True),
    ("LABEL=a:b agent status check", True),
    ("agent log current ''", True),
    ("agent log current 'a ; b | c # d'", True),
    ('agent log current "a ; b | c # d"', True),
    ('agent log current --body="two words"', True),
    ("agent status check 2>&1", True),
    ("agent status check > /dev/null", True),
    ("agent status check 2>/dev/null", True),
    ("agent status check | cat ; agent status check", True),
    ("agent status check && true ; agent status check", True),
    ("agent status check ; true", True),
    ("agent status check # ordinary comment", True),
    ("agent dispatch collect --wait 1\\\n 2", None),  # a continuation inside a word
    ('agent log current "a\\\\b"', True),
    ('agent log current "a\\"b"', True),
]

#: Complete examples (every required argument given) run through `/bin/sh` into a stand-in
#: `agent` that parses with the real CLI parser: the lint fails one exactly when the parser does.
PARSER_DIFFERENTIAL = [
    "agent status check",
    "agent status check --nope",
    "agent status check --json",
    "agent --json status check",
    "agent export /tmp/review-output -m example --remote https://github.com/o/r --json",
    "agent export /tmp/review-output --json -m example --remote https://github.com/o/r",
    "agent --json export /tmp/review-output -m example --remote https://github.com/o/r",
    "agent export /tmp/review-output -mexample --remote https://github.com/o/r",
    "agent export /tmp/review-output -m example --remote https://github.com/o/r",
    "agent dispatch collect --wait=1 --fea=1",
    "agent dispatch collect --wait 1",
    "agent dispatch collect --wait x",
    "agent dispatch nope",
    "agent hook stop -Z",
    "agent hook stop --probe",
    "agent hook not-a-hook",
    "agent '' status check",
    'agent status check ""',
    "agent status --help",
    "agent --version",
    # A missing subcommand is incomplete, but the words present are still judged
    "agent status --nope",
    "agent dispatch --nope",
    "agent feature --nope",
    "agent --nope",
    "agent --json --nope status check",
]


def _argv_shim(tmp_path: Path) -> Path:
    """A stand-in `agent` that appends its argv to `$ARGV_OUT`, read back by `_shim_argvs`. A shell
    script, so no interpreter starts per example."""
    shim = tmp_path / "bin" / "agent"
    shim.parent.mkdir()
    shim.write_text(
        '#!/bin/sh\n{ printf \'\\035\'; for a in "$@"; do printf \'%s\\036\' "$a"; done; } >> "$ARGV_OUT"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return shim


def _shim_argvs(out: Path) -> list[list[str]]:
    if not out.exists():
        return []
    return [record.split("\036")[:-1] for record in out.read_text(encoding="utf-8").split("\035")[1:]]


@pytest.fixture(scope="module")
def shell_argvs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, list[list[str]]]:
    """What `/bin/sh` hands the stand-in `agent` for every example the differentials run. One shell
    runs them all, each in its own subshell with its own `$ARGV_OUT`, one level down so
    `../bin/agent` is the shim."""
    base = tmp_path_factory.mktemp("sh-differential")
    shim = _argv_shim(base)
    (base / "sub" / "ordinary").mkdir(parents=True)
    (base / "sub" / "ordinary" / "agent").symlink_to(shim)  # `./ordinary/agent`
    examples = sorted(
        {
            *PARSER_DIFFERENTIAL,
            *(f"agent {e}" for e, v in DIFFERENTIAL if v),
            *(e for e, v in DIFFERENTIAL_FULL if v),
        }
    )
    script = "".join(
        f"ARGV_OUT={shlex.quote(str(base / f'argv-{i}'))}; export ARGV_OUT\n(\n"
        f"{example.replace('{shim}', str(shim))}\n)\n"
        for i, example in enumerate(examples)
    )
    env = {**os.environ, "PATH": f"{shim.parent}{os.pathsep}{os.environ.get('PATH', '')}"}
    subprocess.run(["/bin/sh", "-c", script], env=env, cwd=base / "sub", capture_output=True, timeout=60)
    found = {example: _shim_argvs(base / f"argv-{i}") for i, example in enumerate(examples)}
    return found | {SHIM_KEY: [[str(shim)]]}


SHIM_KEY = "{shim}"


@pytest.mark.skipif(sys.platform == "win32" or not Path("/bin/sh").exists(), reason="needs /bin/sh")
@pytest.mark.parametrize("example", PARSER_DIFFERENTIAL)
def test_the_lint_agrees_with_the_parser(shell_argvs: dict[str, list[list[str]]], example: str):
    """The lint judges with the real parser, so on a complete example its
    verdict and the CLI's own exit status agree (`-mexample` passes, a misplaced `--json`
    fails)."""
    # The shell's argv goes through the real parser in-process; only the tokenizing is /bin/sh's.
    codes = [_real_exit(argv) for argv in shell_argvs[example]]
    assert len(codes) == 1, example
    assert bool(command_problems(example)) == (codes[0] != 0), (example, codes, command_problems(example))


@pytest.mark.parametrize("fenced", [True, False])
def test_docstrings_and_descriptions_read_code_like_markdown(tmp_path: Path, fenced: bool):
    """String constants, docstrings and tool descriptions go through the same
    extraction as Markdown (`source_code`), so a fence there is checked like a span."""
    bad = "agent status check --nope"
    example = f"Example:\n```sh\n{bad}\n```\n" if fenced else f"Example: `{bad}`."
    source = tmp_path / "sample.py"
    source.write_text(f'def sample():\n    """{example}"""\n', encoding="utf-8")
    assert any("unrecognized arguments: --nope" in p for p in python_problems(source))
    assert any("unrecognized arguments: --nope" in p for p in description_problems({"dispatch": example}))
    indented = tmp_path / "indented.py"
    indented.write_text(
        'def sample():\n    """Summary.\n\n    ```sh\n    agent status check --nope\n    ```\n    """\n',
        encoding="utf-8",
    )
    assert any("unrecognized arguments: --nope" in p for p in python_problems(indented))


def test_an_indented_code_block_in_a_source_is_checked(tmp_path: Path):
    """Only the indentation the Python source added is removed, so an indented
    code block, even a string's only body block, stays one and is checked; a tool description
    is read exactly as the client gets it."""
    description = "Summary.\n\n    agent status check --nope\n"
    assert any("unrecognized arguments: --nope" in p for p in description_problems({"dispatch": description}))
    docstring = tmp_path / "docstring.py"
    docstring.write_text(
        'def sample():\n    """Summary.\n\n        agent status check --nope\n    """\n', encoding="utf-8"
    )
    assert any("unrecognized arguments: --nope" in p for p in python_problems(docstring))
    literal = tmp_path / "literal.py"
    literal.write_text(
        'def sample():\n    return """Summary.\n\n        agent status check --nope\n    """\n',
        encoding="utf-8",
    )
    assert any("unrecognized arguments: --nope" in p for p in python_problems(literal))
    prose = tmp_path / "prose.py"  # the body's own indentation is not a code block
    prose.write_text(
        'def sample():\n    """Summary.\n\n    agent status check --nope\n    """\n', encoding="utf-8"
    )
    assert python_problems(prose) == []
    # Escaped newlines in a one-line literal or a description lose nothing.
    escaped = tmp_path / "escaped.py"
    escaped.write_text(
        'A = "Intro.\\n\\n    agent status check --nope\\n"\n'
        'B = "Intro.\\n```sh\\nagent status check --nope\\n```"\n',
        encoding="utf-8",
    )
    assert sum("unrecognized arguments: --nope" in p for p in python_problems(escaped)) == 2
    assert any(
        "--nope" in p
        for p in description_problems({"dispatch": "Intro.\n```sh\nagent status check --nope\n```"})
    )


PARITY = {
    "unwrapped": "Call the `never_exists` MCP tool.\n",
    "name_wrap": "Call the `never_exists`\nMCP tool.\n",
    "mcp_wrap": "Call the `never_exists` MCP\ntool.\n",
    "blockquote_wrap": "> Call the `never_exists` MCP\n> tool.\n",
    "list_wrap": "- Call the `never_exists` and `never_either` MCP\n  tools.\n",
    "emphasised_tool": "Call the `never_exists` MCP **tool**.\n",
    "emphasised_mcp": "Call the `never_exists` **MCP** tool.\n",
    "snake_wrap": "Call the never_exists MCP\ntool.\n",
    "bold_name": "Call the **`never_exists`** tool.\n",
    "text_fence": "Example instructions:\n\n```text\nCall the `never_exists` tool.\n```\n",
    "text_fence_snake": "Example instructions:\n\n```text\nCall the never_exists tool.\n```\n",
    "indented": "Example instructions:\n\n    Call the `never_exists` tool.\n",
    "generic": "Choose the appropriate MCP tool.\n",
    "valid": "Call the `dispatch` MCP\ntool.\n",
    "residual": "Call the nonesuch MCP tool.\n",
    # A code line is inline content only; action keywords in any case.
    "fence_indented_line": "Steps:\n\n```text\n    Call the `never_exists` tool.\n```\n",
    "fence_nested_list": "Steps:\n\n```text\n- Read the plan.\n  - Call the `never_exists` tool.\n```\n",
    "action_capitalised": "Action `never_exists` is available.\n",
    "action_heading": "# Action `never_exists`\n",
    "tool_has_capital_action": "The `dispatch` tool has Action `land`.\n",
    "action_upper": "Use ACTION `never_exists`.\n",
    "action_valid": "Action `doctor` is available.\n",
    # One normalized text for every reference: markup never splits one.
    "action_bold": "**Action** `never_exists` is available.\n",
    "action_linked": "[Action](#actions) `never_exists` is available.\n",
    "tool_bold_action": "The `dispatch` tool has **action** `land`.\n",
    "action_colon": "Use the `dispatch` tool, action: `land`.\n",
    "action_dash": "Use action — `never_exists`.\n",
    "fence_bold_action": "Steps:\n\n```text\n    **Action** `never_exists` is available.\n```\n",
    "action_colon_valid": "Use the `harness` tool, action: `land`.\n",
    "action_bold_valid": "Use the `harness` tool, **action** `land`.\n",
    "linked_tools_valid": "Use the [`dispatch`](#d) and **`harness`** MCP\ntools.\n",
    # A keyword set as code (`action`, `tool`, `MCP tool`) is still the keyword.
    "code_field": "Use the `dispatch` tool with `action`: `land`.\n",
    "code_keyword": "The `dispatch` tool has `action` `land`.\n",
    "code_field_unknown": "Use `action`: `never_exists`.\n",
    "bold_code_field": "Use the `dispatch` tool with **`action`**: `land`.\n",
    "fenced_code_field": "Steps:\n\n```text\nUse the `dispatch` tool with `action`: `land`.\n```\n",
    "json_field": 'Call the `dispatch` tool with `{"action": "land"}`.\n',
    "code_tool": "Use the `never_exists` `tool`.\n",
    "code_mcp_tool": "Use the `never_exists` `MCP tool`.\n",
    "code_field_valid": "Use the `harness` tool with `action`: `land`.\n",
    "code_tool_valid": "Use the `dispatch` `MCP` `tool`.\n",
    # Every name of an action list is judged, after a tool or alone.
    "action_list_or": "Use action `doctor` or `never_exists`.\n",
    "action_list_three": "Use actions `never_exists`, `doctor` and `nope_x`.\n",
    "tool_actions_list": "The `dispatch` tool has actions `new`, `land`.\n",
    "tool_actions_slash": "The `dispatch` tool, actions `new` / `land`.\n",
    "action_list_valid": "The `harness` tool has actions `doctor` and `land`.\n",
    "action_then_params_valid": 'Call the `peer` tool with `action="send"`, `to=<name>`.\n',
    "action_then_tool_valid": "Use action `doctor` and the `harness` tool.\n",
    # An action list ends at a key; only the `action` key's value is an action.
    "json_parameters_valid": (
        'Call the `peer` tool with `{"action": "send", "to": "feature-1", "message": "hello"}`.\n'
    ),
    "code_parameters_valid": (
        "Call the `peer` tool with `action`: `send`, `to`: `feature-1`, `message`: `hello`.\n"
    ),
    "json_parameters_bad": 'Call the `peer` tool with `{"action": "never_exists", "to": "x"}`.\n',
    "keyword_value": "Use action `tools`.\n",
    # A name before ":" is a key only when a value follows the colon.
    "list_colon_prose": "The `dispatch` tool has actions `new` and `land`: choose one.\n",
    "json_values_valid": 'Call the `peer` tool with `{"action": "send", "count": 3, "ok": true}`.\n',
    "field_values_valid": "Call the `peer` tool with `action`: `send`, `to`: `feature-1`.\n",
    # Structure, not what follows a colon. A code span holding a mapping is
    # parsed; a field is `action` set as code; a prose list runs to its colon.
    "list_colon_code": "The `dispatch` tool has actions `new` and `land`: `new` starts a worker.\n",
    "list_colon_number": "The `dispatch` tool has actions `new` and `land`: 2 ways to start work.\n",
    "list_colon_valid": "The `dispatch` tool has actions `new` and `resume`: `new` starts a worker.\n",
    "json_null_valid": 'Call the `dispatch` tool with `{"action": "list", "feature": null}`.\n',
    "json_empty_list_valid": (
        'Call the `dispatch` tool with `{"action": "new", "writes": [], "repo": "harness", '
        '"prompt": "Review the branch", "no_checks": true, "dry_run": true}`.\n'
    ),
    "json_string_valid": 'Call the `dispatch` tool with `{"action": "list", "feature": "001"}`.\n',
    "fields_empty_list_valid": "The `dispatch` tool takes `action`: `new`, `writes`: [].\n",
    "kwargs_valid": 'Call the `peer` tool with `action="send", to="x"`.\n',
    "kwargs_bad": 'Call the `peer` tool with `action="never_exists", to="x"`.\n',
    "json_alone_bad": 'Use `{"action": "never_exists"}`.\n',
    # A mapping keeps its tool however the tool is named; a quoted prose list
    # runs to its colon like a code one.
    "json_mcp_bad": 'Call the `dispatch` MCP tool with `{"action": "land"}`.\n',
    "kwargs_mcp_bad": 'Call the `dispatch` MCP tool with `action="land"`.\n',
    "kwargs_short_bad": 'Call `dispatch` with `action="land"`.\n',
    "json_colon_bad": 'Call the `dispatch` tool: `{"action": "land"}`.\n',
    "json_plain_bad": 'Call the `dispatch` tool with `{"action": "land"}`.\n',
    "kwargs_plain_bad": 'Call the `dispatch` tool with `action="land"`.\n',
    "json_mcp_valid": 'Call the `dispatch` MCP tool with `{"action": "list", "feature": null}`.\n',
    "json_colon_valid": 'Call the `dispatch` tool: `{"action": "list", "feature": null}`.\n',
    "kwargs_short_valid": 'Call `harness` with `action="scratch_path"` once.\n',
    "fields_noncode_valid": "Call the `peer` tool with action: `send`, `to`: `feature-1`.\n",
    "quoted_list_colon_bad": 'The `dispatch` tool has actions "new" and "land": choose one.\n',
    "single_list_colon_bad": "The `dispatch` tool has actions 'new' and 'land': choose one.\n",
    "quoted_list_code_bad": 'The `dispatch` tool has actions "new" and "land": "new" starts a worker.\n',
    "quoted_list_colon_valid": 'The `dispatch` tool has actions "new" and "resume": choose one.\n',
    # A field's value list runs to the next key: every joined name is judged.
    "field_list_bad": "Use action: `doctor` or `never_exists`.\n",
    "field_list_tool_bad": "Use the `dispatch` tool, action: `new` / `land`.\n",
    "field_list_valid": "Use the `harness` tool with `action`: `doctor` or `land`.\n",
    # A parsed action ends its list; field colons need values; mapping
    # strings are judged whole, including spaces.
    "kwargs_fields_valid": 'Call the `peer` tool with `action="send"`, `to`: `feature-1`.\n',
    "kwargs_params_valid": 'Call the `peer` tool with `action="send"`, `to` and `message`.\n',
    "json_fields_valid": 'Call the `peer` tool with `{"action": "send"}`, `to`: `feature-1`.\n',
    "json_params_valid": 'Call the `peer` tool with `{"action": "send"}`, `to` and `message`.\n',
    "kwargs_equals_valid": 'Call the `peer` tool with `action="send"`, `to` = `feature-1`.\n',
    "field_list_colon_bad": "Use the `dispatch` tool, action: `new` or `land`: choose one.\n",
    "field_list_colon_valid": "Use the `dispatch` tool, action: `new` or `resume`: choose one.\n",
    "json_space_bad": 'Call the `dispatch` tool with `{"action": "list all"}`.\n',
    "json_trailing_space_bad": 'Call the `dispatch` tool with `{"action": "list "}`.\n',
    "json_list_valid": 'Call the `dispatch` tool with `{"action": "list"}`.\n',
    "kwargs_empty_bad": 'Call the `dispatch` tool with `action=""`.\n',
    "json_space_alone_bad": 'Use `{"action": "list all"}`.\n',
    "field_string_valid": 'Call the `peer` tool with action: `send`, `to`: "feature-1".\n',
    "field_number_valid": "Call the `peer` tool with action: `send`, `count`: -.5.\n",
    "field_true_valid": "Call the `peer` tool with action: `send`, `ok`: true.\n",
    "field_false_valid": "Call the `peer` tool with action: `send`, `ok`: false.\n",
    "field_null_valid": "Call the `peer` tool with action: `send`, `to`: null.\n",
    "field_object_valid": "Call the `peer` tool with action: `send`, `to`: {}.\n",
    "field_bare_valid": "Call the `peer` tool with action: `send`, `to`: target, `message`: `hello`.\n",
    # Later mapping spans keep the first span's tool; alternatives before
    # a colon stay actions, while comma/semicolon-separated fields can be parameter keys.
    "kwargs_pair_bad": 'Call the `dispatch` tool with `action="new"` or `action="land"`.\n',
    "kwargs_pair_and_bad": 'Call the `dispatch` tool with `action="new"` and `action="land"`.\n',
    "kwargs_pair_slash_bad": 'Call the `dispatch` tool with `action="new"` / `action="land"`.\n',
    "kwargs_pair_comma_bad": 'Call the `dispatch` tool with `action="new"`, `action="land"`.\n',
    "kwargs_pair_wrap_bad": (
        'Call the `harness` tool with\n`action="notes_root"` and `action="list"` to identify the file.\n'
    ),
    "kwargs_pair_valid": 'Call the `harness` tool with `action="doctor"` or `action="land"`.\n',
    "kwargs_pair_tool_valid": (
        'Call the `dispatch` tool with `action="new"` or the `harness` tool with `action="land"`.\n'
    ),
    # A bare tool name ends inheritance too.
    "kwargs_pair_bare_tool_valid": (
        'Use the dispatch tool with `action="new"`; once it finishes, the harness tool with `action="land"`.\n'
    ),
    "kwargs_pair_bare_possessive_valid": (
        'Call the `dispatch` tool with `action="new"` or the harness tool\'s `action="land"`.\n'
    ),
    "kwargs_pair_bare_tool_bad": (
        'Call the `dispatch` tool with `action="new"`, or the harness tool with `action="lnd"`.\n'
    ),
    "kwargs_pair_sentence_valid": 'Call the `dispatch` tool with `action="new"`. Use `action="land"`.\n',
    "json_pair_bad": 'Call the `dispatch` tool with `{"action": "new"}` or `{"action": "land"}`.\n',
    "mixed_pair_bad": 'Call the `dispatch` tool with `{"action": "new"}` or `action="land"`.\n',
    "field_list_code_bad": "Use the `dispatch` tool, action: `new` or `land`: `new` starts a worker.\n",
    "field_list_and_code_bad": "Use the `dispatch` tool, action: `new` and `land`: `new` starts a worker.\n",
    "field_list_slash_code_bad": "Use the `dispatch` tool, action: `new` / `land`: `new` starts a worker.\n",
    "field_list_code_valid": "Use the `dispatch` tool, action: `new` or `resume`: `new` starts a worker.\n",
    "field_semicolon_valid": "Call the `peer` tool with action: `send`; `to`: `x`.\n",
    "code_list_colon_bad": 'Use the `dispatch` tool, `action: "new" or "land": "new" starts a worker`.\n',
    "code_list_equals_bad": 'Use the `dispatch` tool, `action: "new" / "land"="new" starts a worker`.\n',
    "code_key_valid": 'Call the `peer` tool with `action: "send", "to": "x"`.\n',
    "code_semicolon_key_valid": 'Call the `peer` tool with `action: "send"; "to": "x"`.\n',
    "kwargs_next_parameter_valid": 'Call the `peer` tool with `action="send"` `to`: `x`.\n',
    "kwargs_pair_prose_bad": (
        'Call the `dispatch` tool with `action="new"`, then use `action="land"` to finish.\n'
    ),
    "kwargs_pair_three_bad": (
        'Call the `dispatch` tool with `action="new"`, `action="resume"` or `action="land"`.\n'
    ),
    "kwargs_pair_short_tool_valid": 'Call `dispatch` with `action="new"` or `harness` with `action="land"`.\n',
    "kwargs_pair_paragraph_valid": 'Call the `dispatch` tool with `action="new"`\n\nUse `action="land"`.\n',
    "field_code_value_valid": "Call the `peer` tool with action: `send`, `to`: `x`.\n",
    "code_list_and_bad": 'Use the `dispatch` tool, `action: "new" and "land": "new" starts a worker`.\n',
}
#: The MCP findings each `PARITY` text must give in every source (none when it is not here).
PARITY_FINDINGS = {
    "kwargs_next_parameter_valid": [],
    "kwargs_pair_prose_bad": ["`dispatch` has no action `land`"],
    "kwargs_pair_three_bad": ["`dispatch` has no action `land`"],
    "kwargs_pair_short_tool_valid": [],
    "kwargs_pair_paragraph_valid": [],
    "field_code_value_valid": [],
    "code_list_and_bad": ["`dispatch` has no action `land`"],
    "kwargs_pair_bad": ["`dispatch` has no action `land`"],
    "kwargs_pair_and_bad": ["`dispatch` has no action `land`"],
    "kwargs_pair_slash_bad": ["`dispatch` has no action `land`"],
    "kwargs_pair_comma_bad": ["`dispatch` has no action `land`"],
    "kwargs_pair_wrap_bad": ["`harness` has no action `list`"],
    "kwargs_pair_valid": [],
    "kwargs_pair_tool_valid": [],
    "kwargs_pair_bare_tool_valid": [],
    "kwargs_pair_bare_possessive_valid": [],
    "kwargs_pair_bare_tool_bad": ["`harness` has no action `lnd`"],
    "kwargs_pair_sentence_valid": [],
    "json_pair_bad": ["`dispatch` has no action `land`"],
    "mixed_pair_bad": ["`dispatch` has no action `land`"],
    "field_list_code_bad": ["`dispatch` has no action `land`"],
    "field_list_and_code_bad": ["`dispatch` has no action `land`"],
    "field_list_slash_code_bad": ["`dispatch` has no action `land`"],
    "field_list_code_valid": [],
    "field_semicolon_valid": [],
    "code_list_colon_bad": ["`dispatch` has no action `land`"],
    "code_list_equals_bad": ["`dispatch` has no action `land`"],
    "code_key_valid": [],
    "code_semicolon_key_valid": [],
    "kwargs_fields_valid": [],
    "kwargs_params_valid": [],
    "json_fields_valid": [],
    "json_params_valid": [],
    "kwargs_equals_valid": [],
    "field_list_colon_bad": ["`dispatch` has no action `land`"],
    "field_list_colon_valid": [],
    "json_space_bad": ["`dispatch` has no action `list all`"],
    "json_trailing_space_bad": ["`dispatch` has no action `list `"],
    "json_list_valid": [],
    "kwargs_empty_bad": ["`dispatch` has no action ``"],
    "json_space_alone_bad": ["no MCP tool has action `list all`"],
    "field_string_valid": [],
    "field_number_valid": [],
    "field_true_valid": [],
    "field_false_valid": [],
    "field_null_valid": [],
    "field_object_valid": [],
    "field_bare_valid": [],
    "field_list_bad": ["no MCP tool has action `never_exists`"],
    "field_list_tool_bad": ["`dispatch` has no action `land`"],
    "field_list_valid": [],
    "json_mcp_bad": ["`dispatch` has no action `land`"],
    "kwargs_mcp_bad": ["`dispatch` has no action `land`"],
    "kwargs_short_bad": ["`dispatch` has no action `land`"],
    "json_colon_bad": ["`dispatch` has no action `land`"],
    "json_plain_bad": ["`dispatch` has no action `land`"],
    "kwargs_plain_bad": ["`dispatch` has no action `land`"],
    "json_mcp_valid": [],
    "json_colon_valid": [],
    "kwargs_short_valid": [],
    "fields_noncode_valid": [],
    "quoted_list_colon_bad": ["`dispatch` has no action `land`"],
    "single_list_colon_bad": ["`dispatch` has no action `land`"],
    "quoted_list_code_bad": ["`dispatch` has no action `land`"],
    "quoted_list_colon_valid": [],
    "list_colon_code": ["`dispatch` has no action `land`"],
    "list_colon_number": ["`dispatch` has no action `land`"],
    "list_colon_valid": [],
    "json_null_valid": [],
    "json_empty_list_valid": [],
    "json_string_valid": [],
    "fields_empty_list_valid": [],
    "kwargs_valid": [],
    "kwargs_bad": ["`peer` has no action `never_exists`"],
    "json_alone_bad": ["no MCP tool has action `never_exists`"],
    "list_colon_prose": ["`dispatch` has no action `land`"],
    "json_values_valid": [],
    "field_values_valid": [],
    "json_parameters_valid": [],
    "code_parameters_valid": [],
    "json_parameters_bad": ["`peer` has no action `never_exists`"],
    "keyword_value": ["no MCP tool has action `tools`"],
    "generic": [],
    "valid": [],
    "residual": [],
    "action_valid": [],
    "action_colon_valid": [],
    "action_bold_valid": [],
    "linked_tools_valid": [],
    "code_field_valid": [],
    "code_tool_valid": [],
    "action_list_valid": [],
    "action_then_params_valid": [],
    "action_then_tool_valid": [],
    "action_list_or": ["no MCP tool has action `never_exists`"],
    "action_list_three": ["no MCP tool has action `never_exists`", "no MCP tool has action `nope_x`"],
    "tool_actions_list": ["`dispatch` has no action `land`"],
    "tool_actions_slash": ["`dispatch` has no action `land`"],
    "code_field": ["`dispatch` has no action `land`"],
    "code_keyword": ["`dispatch` has no action `land`"],
    "code_field_unknown": ["no MCP tool has action `never_exists`"],
    "bold_code_field": ["`dispatch` has no action `land`"],
    "fenced_code_field": ["`dispatch` has no action `land`"],
    "json_field": ["`dispatch` has no action `land`"],
    "action_bold": ["no MCP tool has action `never_exists`"],
    "action_linked": ["no MCP tool has action `never_exists`"],
    "action_dash": ["no MCP tool has action `never_exists`"],
    "fence_bold_action": ["no MCP tool has action `never_exists`"],
    "tool_bold_action": ["`dispatch` has no action `land`"],
    "action_colon": ["`dispatch` has no action `land`"],
    "list_wrap": ["`never_either`: no MCP tool by that name", "`never_exists`: no MCP tool by that name"],
    "action_capitalised": ["no MCP tool has action `never_exists`"],
    "action_heading": ["no MCP tool has action `never_exists`"],
    "action_upper": ["no MCP tool has action `never_exists`"],
    "tool_has_capital_action": ["`dispatch` has no action `land`"],
}


@pytest.mark.parametrize("name", sorted(PARITY))
def test_tool_references_are_read_alike_in_every_source(tmp_path: Path, name: str):
    """One text gives the same MCP tool and action findings in a Markdown
    file, a docstring, a tool description and a YAML frontmatter value, its code blocks
    included."""
    text = PARITY[name]
    doc = tmp_path / f"{name}.md"
    doc.write_text(text, encoding="utf-8")
    source = tmp_path / f"{name}.py"
    body = text.rstrip("\n").replace("\n", "\n    ")
    source.write_text(f'def sample():\n    """{body}\n    """\n', encoding="utf-8")
    front = tmp_path / f"{name}-front.md"
    value = text.rstrip("\n").replace("\n", "\n  ")
    front.write_text(f"---\ndescription: |-\n  {value}\n---\n", encoding="utf-8")

    def mcp(problems: list[str]) -> list[str]:
        found = re.findall(
            r"`\w+`: no MCP tool by that name|`\w+` has no action `[^`]*`|no MCP tool has action `[^`]*`",
            "\n".join(problems),
        )
        return sorted(set(found))

    markdown = mcp(markdown_problems(doc))
    assert mcp(python_problems(source)) == markdown
    assert mcp(description_problems({"dispatch": text})) == markdown
    assert mcp(markdown_problems(front)) == markdown
    assert markdown == PARITY_FINDINGS.get(name, ["`never_exists`: no MCP tool by that name"])


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("Call the `never_exists`\n    tool.", "no MCP tool"),
        ("The harness tool has action\n    neverexists.", "has no action `neverexists`"),
        ("Call the `never_exists`\n\n    tool.", None),  # two paragraphs
    ],
)
def test_wrapped_mcp_references_in_sources(tmp_path: Path, text: str, found: str | None):
    """A docstring and a tool description read a wrapped reference as one."""
    name = f"wrapped_{abs(hash(text))}.py"  # its own file: a module's walk is cached by path
    source = tmp_path / name
    source.write_text(f'def sample():\n    """{text}\n    """\n', encoding="utf-8")
    description = text.replace("\n    ", "\n")
    for problems in (python_problems(source), description_problems({"dispatch": description})):
        assert any(found in p for p in problems) if found else problems == [], problems


@pytest.mark.parametrize(
    "example",
    [
        "Run `python -m agent_cli status check --nope`.",
        "Run:\n\n```sh\npython.exe -I -m agent_cli status check --nope\n```\n",
    ],
)
def test_the_module_launcher_is_checked_in_sources(tmp_path: Path, example: str):
    """`python -m agent_cli` is the launcher in a docstring and a tool
    description too, its argv judged like `agent`'s."""
    source = tmp_path / "module.py"
    source.write_text(f'def sample():\n    """{example}"""\n', encoding="utf-8")
    assert any("unrecognized arguments: --nope" in p for p in python_problems(source))
    assert any("unrecognized arguments: --nope" in p for p in description_problems({"dispatch": example}))
    control = example.replace(" --nope", "")
    clean = tmp_path / "control.py"  # its own file: a module's walk is cached by path
    clean.write_text(f'def sample():\n    """{control}"""\n', encoding="utf-8")
    assert python_problems(clean) == []
    assert description_problems({"dispatch": control}) == []


def _leaf_argvs() -> list[list[str]]:
    """Every parser of the CLI as a complete argv (each required argument a dummy), and every
    parser above a leaf as the words that reach it."""
    out: list[list[str]] = []

    def walk(current: argparse.ArgumentParser, prefix: list[str]) -> None:
        subs = [a for a in current._actions if isinstance(a, argparse._SubParsersAction)]  # pyright: ignore[reportPrivateUsage]
        if subs:
            out.append(prefix)
            for name, child in subs[0].choices.items():  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
                walk(child, [*prefix, str(name)])  # pyright: ignore[reportUnknownArgumentType]
            return
        args = list(prefix)
        for action in current._actions:  # pyright: ignore[reportPrivateUsage]
            if (action.option_strings and not action.required) or action.nargs in (
                0,
                "?",
                "*",
                argparse.REMAINDER,
            ):
                continue
            count = action.nargs if isinstance(action.nargs, int) else 1
            args += [action.option_strings[0]] if action.option_strings else []
            convert: Any = current._registry_get("type", action.type, action.type)  # pyright: ignore[reportPrivateUsage]
            args += [str(_dummy(action, convert if callable(convert) else None))] * count
        out.append(args)

    walk(parser(), [])
    return out


def _real_exit(argv: list[str]) -> int:
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def test_every_parser_level_agrees_with_the_real_parser():
    """At every level of the CLI, a complete argv passes exactly when the real
    parser accepts it, and an unknown option added at that level always fails, whether or not
    the subcommand below it is given (`agent status --nope`)."""
    mismatches: list[tuple[list[str], int, str | None]] = []
    checked = 0
    for argv in _leaf_argvs():
        complete = _real_exit(argv) == 0
        for case in ([argv] if complete else []) + [[*argv, "--nope"]]:
            checked += 1
            found = _parse(tuple(case))
            if bool(found) != bool(_real_exit(case)):
                mismatches.append((case, _real_exit(case), found))
    assert checked > 100
    assert mismatches == []


@pytest.mark.skipif(sys.platform == "win32" or not Path("/bin/sh").exists(), reason="needs /bin/sh")
@pytest.mark.parametrize(
    ("example", "verifiable"),
    [*((f"agent {e}", v) for e, v in DIFFERENTIAL), *DIFFERENTIAL_FULL],
)
def test_the_tokenizer_agrees_with_sh(
    shell_argvs: dict[str, list[list[str]]], example: str, verifiable: bool | None
):
    """For every row the lint accepts, the argv of each command that runs
    the CLI is exactly what `/bin/sh` gives a stand-in `agent` (one that only records its
    arguments), empty arguments included; every other row is refused as not checkable."""
    ran = shell_argvs.get(example)
    example = example.replace(SHIM_KEY, shell_argvs[SHIM_KEY][0][0])  # an absolute path to the launcher
    lines = example_lines(example)
    assert lines, example
    if verifiable is None:
        assert any(line.unverifiable for line in lines), f"{example!r} must fail closed"
        assert any(CANNOT_VERIFY in why for _n, why in command_problems(example)), example
        return
    assert not any(line.unverifiable for line in lines), example
    lint_argvs = [[str(w) for w in argv] for line in lines for argv in line.argvs]
    assert lint_argvs == ran, example


def test_tool_descriptions_are_checked_for_tools_and_actions():
    problems = description_problems(
        {"harness": "Call the `never_exists` tool, or `lease` with action `nope`."}
    )
    assert any("never_exists" in p for p in problems) and any("`nope`" in p for p in problems)


def test_hook_configs_are_checked(tmp_path: Path):
    config = tmp_path / "hooks.json"
    wrap = 'node -e "require(\\"x\\")" '
    for command, found in [
        (f"{wrap}agent hook no-such-hook", "invalid choice: 'no-such-hook'"),
        (f"{wrap}agent hook stop -Z", "unrecognized arguments: -Z"),
        (f"{wrap}agent hook stop ; agent hook nope-x", "invalid choice: 'nope-x'"),
        ("node run.cjs agent hook stop", CANNOT_VERIFY),  # not the wrapper: refused, not guessed
        ("node run.cjs", "no `agent` command"),
    ]:
        config.write_text(json.dumps({"hooks": {"Stop": [{"command": command}]}}))
        assert any(found in p for p in hook_config_problems(config)), command
    for fine in (f'{wrap}agent hook "stop"', "agent hook stop", f"{wrap}agent hook stop --probe"):
        config.write_text(json.dumps({"hooks": {"Stop": [{"command": fine}]}}))
        assert hook_config_problems(config) == [], fine


def test_every_docstring_is_checked(tmp_path: Path):
    """Function and class docstrings, not only the module's."""
    text = '"""Read `harness/never-exists.md`; call `export.render_adr_index`."""'
    for i, source in enumerate((f"{text}\n", f"def f():\n    {text}\n", f"class C:\n    {text}\n")):
        module = tmp_path / f"probe{i}.py"  # a new name each time: the AST caches are per path
        module.write_text(source)
        problems = python_problems(module)
        assert any("harness/never-exists.md" in p for p in problems), source
        assert any("export.render_adr_index" in p for p in problems), source


def test_unquoted_paths_are_checked_in_docstrings_and_descriptions(tmp_path: Path):
    """One path check (`path_problems`) for every context."""
    module = tmp_path / "unquoted.py"
    module.write_text('def f():\n    """Read harness/never-exists.md first."""\n')
    assert any("harness/never-exists.md" in p for p in python_problems(module))
    assert any(
        "harness/never-exists.md" in p
        for p in description_problems({"harness": "Read harness/never-exists.md first."})
    )
    assert description_problems({"harness": "Read harness/maintenance.md first."}) == []


def test_the_checks_catch_the_items_audit_6_found():
    assert check_path("doctor/checks_vault.py", SRC / "vault" / "__init__.py") is not None
    assert check_path("tests/test_hooks.py", SRC) is not None
    assert check_path("harness/owner/profile.md", SRC) is None  # notes-linked
    assert check_symbol("dispatch.nope_never_defined") is not None
    assert check_symbol("status.render_adr_index") is None
    assert check_symbol("export.render_adr_index") is not None  # defined in status, not export
    assert check_symbol("dispatch.state.POLL_SECONDS") is None
    assert check_symbol("dispatch.state.NOPE_NEVER") is not None
