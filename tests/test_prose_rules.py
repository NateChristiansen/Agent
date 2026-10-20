"""Prose rules a test can hold. `harness/rules-enforcement.md` maps each of
the twelve prose rules map to its check, or says why it stays prose.

A fix batch corrected six hand copies of a code list that had drifted (the README
doctor list, PRIVATE_SET, the `.agent` deny list, the journal fields, the Obsidian views and
the worktree suffix). They matched again only because someone fixed them by hand. These tests
pin the copies that are lists, against the code they copy.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from agent_cli import core, links
from agent_cli.hooks.run import HANDLERS
from agent_cli.hooks.toggles import TOGGLEABLE
from agent_cli.journal.block import meta_for

ROOT = Path(__file__).resolve().parents[1]

HISTORY_TAG = re.compile(
    r"\baudit \d|\bCL-\d|\bCX-\d|\bT\d+ item|\bL-20\d{6}|\bfeature 0\d\d\b",
    re.IGNORECASE,
)


def history_tags(source: str) -> list[tuple[int, str]]:
    """Find provenance in comments and documentation strings, excluding runtime text."""
    documentation: dict[tuple[int, int], ast.Constant] = {}
    lines = source.split("\n")
    for node in ast.walk(ast.parse(source)):
        # Include attribute and additional docstrings as well as first statements.
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            value = node.value
            if isinstance(value.value, str):
                column = len(lines[value.lineno - 1].encode()[: value.col_offset].decode())
                documentation[value.lineno, column] = value
    found: list[tuple[int, str]] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            text = token.string
        elif token.type == tokenize.STRING and (doc := documentation.get(token.start)) is not None:
            text = str(doc.value)
        else:
            continue
        found.extend((token.start[0], match.group()) for match in HISTORY_TAG.finditer(text))
    return found


def test_source_prose_has_no_history_tags():
    found = {
        path.relative_to(ROOT).as_posix(): hits
        for path in sorted((ROOT / "src").rglob("*.py"))
        if (hits := history_tags(path.read_text(encoding="utf-8")))
    }
    assert not found, f"history belongs in git/ADRs; write the current why: {found}"


@pytest.mark.parametrize("tag", ["audit 6", "CL-64", "CX-4", "T7 item 2", "L-20261005-09", "feature 013"])
@pytest.mark.parametrize(
    "template",
    [
        "# {tag}\n",
        '"""{tag}"""\n',
        'class Example:\n    """{tag}"""\n',
        'def example():\n    """{tag}"""\n',
        'async def example():\n    """{tag}"""\n',
        'value = 1\n"""{tag}"""\n',
    ],
)
def test_history_guard_covers_comments_and_docstrings(tag: str, template: str):
    assert history_tags(template.format(tag=tag))


@pytest.mark.parametrize(
    "source",
    [
        'message = "audit 6"\n',
        'raise ValueError("feature 013")\n',
        'def example():\n    return "CL-64"\n',
        'message = "# CX-4"\n',
        '"""See ADR-0012; hold the lock before writing."""\n',
    ],
)
def test_history_guard_leaves_runtime_text_and_current_contracts(source: str):
    assert history_tags(source) == []


def test_history_guard_reads_complete_decoded_docstrings():
    assert history_tags('"""Details.\\nAUDIT 6"""\n')
    assert history_tags('"""\\x61udit 6"""\n')
    assert history_tags('("audit " "6")\n')
    assert history_tags('é = 1; "audit 6"\n')


def _names(text: str) -> set[str]:
    return set(re.findall(r"`([a-z][a-z-]*)`", text))


def _section(path: str, start: str, end: str) -> str:
    text = (ROOT / path).read_text(encoding="utf-8")
    return text[text.index(start) : text.index(end, text.index(start))]


def test_the_removable_and_protected_hook_lists_match_the_code():
    """Row 6: `harness/agent-yaml.md` lists `toggles.TOGGLEABLE`, and `harness/standing-rules.md`
    lists the rest of `run.HANDLERS`."""
    protected = _section("harness/standing-rules.md", "Protected core", "A leftover switch")
    assert _names(protected) == set(HANDLERS) - set(TOGGLEABLE)
    row = _section("harness/agent-yaml.md", "| `hooks` |", "\n")
    listed = set(re.findall(r"[a-z]+(?:-[a-z]+)+", row.split("Only ", 1)[1].split("can be switched")[0]))
    assert listed == set(TOGGLEABLE)


def test_the_obsidian_views_list_matches_the_scripts():
    """Row 7: the `harness/obsidian/views/*.js` row in `harness/obsidian.md` names every script."""
    row = _section("harness/obsidian.md", "| `harness/obsidian/views/*.js` |", "\n")
    scripts = {p.stem for p in (ROOT / "harness" / "obsidian" / "views").glob("*.js")}
    assert _names(row.split("scripts:", 1)[1]) == scripts


def test_every_journal_property_is_documented():
    """Row 7: each property the CLI writes on a daily note is described in `harness/journal.md`,
    the conditional ones included (`usage_machines`, `tokens_complete`, `probe_spend_usd`)."""
    assert undocumented_journal_properties(ROOT / "harness" / "journal.md") == []


def journal_properties() -> set[str]:
    day = date(2026, 1, 14)
    bare = meta_for(day, None)
    full = meta_for(
        day,
        None,
        spend_usd=1.0,
        dispatches=1,
        session_tokens=1,
        session_models="m",
        probe_spend_usd=1.0,
        manager_usd=1.0,
        spend_complete=False,
        manager_complete=False,
        usage_machines={},
        tokens_complete=False,
    )
    return (set(bare) | set(full)) - {"tags", "date"}


def undocumented_journal_properties(doc: Path) -> list[str]:
    text = doc.read_text(encoding="utf-8")
    return sorted(p for p in journal_properties() if f"`{p}`" not in text)


def test_the_journal_check_covers_conditional_properties(tmp_path: Path):
    """`probe_spend_usd` is written only on a day with probe
    spend, and the check sampled a day without it."""
    assert {"probe_spend_usd", "usage_machines", "tokens_complete"} <= journal_properties()
    doc = tmp_path / "journal.md"
    text = (ROOT / "harness" / "journal.md").read_text(encoding="utf-8")
    doc.write_text(text.replace("`probe_spend_usd`", "`removed`"), encoding="utf-8")
    assert undocumented_journal_properties(doc) == ["probe_spend_usd"]


# ---------------------------------------------------------------- row 3: `.agent/` subtrees
#
# A new `.agent/` subtree must be registered (`core.AGENT_SUBTREES`, protected) or listed as
# deliberately unregistered (`core.AGENT_UNREGISTERED`) before code can use it. Three layers,
# each enforcing a stated part `harness/rules-enforcement.md`
# row 3):
#
# 1. Runtime: `core.agent_path`/`agent_parts` raise for an unregistered subtree or a component
#    that leaves it (`..`, a separator, a drive); `agent_root(root) / x` is checked the same way.
# 2. Static, below: outside `core.py`, no `.agent` literal (str, f-string part, bytes), no
#    `AGENT_DIR`, and no `agent_dir_name()` in a path expression. Prose may mention `.agent/...`
#    only in a docstring or as a direct argument of help=/description=/epilog=, an exception
#    (`AgentError(...)`, `raise ...`) or a tool description (`ToolSpec(...)`). Concatenation,
#    `chr()` and other computed spellings are out of its reach on purpose.
# 3. Dynamic, `tests/conftest.py`: at the end of every test session, each directory directly
#    under any `.agent/` in pytest's basetemp must be registered. It catches every spelling,
#    computed ones included, in the code the tests run.

#: `.agent` as a path, not as part of `.agents/`, `mcp_servers.agent` or `.agent-pre-push-`.
_AGENT_LITERAL = re.compile(r"(?<![\w.])\.agent(?![\w-])")
_AGENT_BYTES = re.compile(rb"(?<![\w.])\.agent(?![\w-])")
#: Where the name may be spelled: the registry and its helpers.
_SPELLED_IN = {"src/agent_cli/core.py"}
_SINK_KEYWORDS = {"help", "description", "epilog"}
_SINK_CALL = re.compile(r"(?:Error|Failure|Exception|NotFound|ToolSpec)$")
_PATH_CALLS = {"Path", "PurePath", "PurePosixPath", "PureWindowsPath", "joinpath", "join"}


def _call_name(node: ast.AST) -> str:
    func = node.func if isinstance(node, ast.Call) else None
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""


def agent_literals(source: str) -> list[tuple[int, str]]:
    """Each place a module spells the `.agent` folder outside the helpers, with its line."""
    tree = ast.parse(source)
    parent: dict[int, ast.AST] = {id(c): n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}

    def in_sink(node: ast.AST) -> bool:
        """A docstring, or text that is a direct argument of a prose sink (through f-strings
        and `+`)."""
        up = parent.get(id(node))
        if isinstance(up, ast.Expr):
            return True  # a docstring, or a string statement that documents the line above
        while isinstance(up, ast.JoinedStr | ast.FormattedValue) or (
            isinstance(up, ast.BinOp) and isinstance(up.op, ast.Add)
        ):
            node, up = up, parent.get(id(up))
        if isinstance(up, ast.keyword):
            return up.arg in _SINK_KEYWORDS
        return isinstance(up, ast.Call) and node in up.args and bool(_SINK_CALL.search(_call_name(up)))

    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Name) and node.id == "AGENT_DIR":
            out.append((line, "AGENT_DIR"))
        elif isinstance(node, ast.Attribute) and node.attr == "AGENT_DIR":
            out.append((line, "AGENT_DIR"))
        elif isinstance(node, ast.ImportFrom) and any(a.name == "AGENT_DIR" for a in node.names):
            out.append((line, "import AGENT_DIR"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, bytes):
            if _AGENT_BYTES.search(node.value):
                out.append((line, repr(node.value[:80])))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _AGENT_LITERAL.search(node.value) and not in_sink(node):
                out.append((line, node.value[:80]))
        elif isinstance(node, ast.Call) and _call_name(node) == "agent_dir_name":
            up = parent.get(id(node))
            in_path = (isinstance(up, ast.BinOp) and isinstance(up.op, ast.Div)) or (
                isinstance(up, ast.Call) and _call_name(up) in _PATH_CALLS
            )
            if in_path:
                out.append((line, "agent_dir_name() in a path"))
    return out


def test_no_module_but_core_spells_the_agent_folder():
    found = {
        path.relative_to(ROOT).as_posix(): hits
        for path in sorted((ROOT / "src" / "agent_cli").rglob("*.py"))
        if path.relative_to(ROOT).as_posix() not in _SPELLED_IN
        and (hits := agent_literals(path.read_text(encoding="utf-8")))
    }
    assert found == {}, (
        "build `.agent/` paths with core.agent_path / agent_parts / agent_root, "
        "and write them in messages with core.agent_display"
    )


@pytest.mark.parametrize(
    "source",
    [
        'p = Path(".") / ".agent" / "new-x"\n',
        "p = Path('.') / '.agent' / 'new-x'\n",
        'state = root / ".agent"\np = state / "new-x"\n',
        'p = Path(".agent", "new-x")\n',
        'p = Path(".").joinpath(".agent", "new-x")\n',
        'p = os.path.join(root, ".agent", "new-x")\n',
        'lock((".agent", "new-x", "x.lock"))\n',
        'p = f"{root}/.agent/new-x/y"\n',
        'p = Path(".agent/new-x")\n',
        'p = Path("%(s)s/new-x" % {"s": ".agent"})\n',
        # Review fixtures: a try with a raise in between, a value carried round a loop, a
        # comprehension over known paths.
        'def f(root):\n    try:\n        state = Path(".agent")\n        boom()\n        state = Path("x")\n'
        "    except OSError:\n        return state / 'new-x'\n",
        'a = b = c = None\nfor _ in range(4):\n    c, b, a = b, a, Path(".agent")\np = c / "new-x"\n',
        'paths = [state / "new-x" for state in [Path(".agent")]]\n',
        # Review static fixtures: the imported name, bytes, and prose-looking paths.
        "from agent_cli.core import AGENT_DIR\np = root / AGENT_DIR / 'new-x'\n",
        "from agent_cli import core\np = root / core.AGENT_DIR / 'new-x'\n",
        'p = root / b".agent".decode() / "new-x"\n',
        'p = root / ".agent/new-x/probe file"\n',
        'p = root / " .agent/new-x".strip()\n',
        'x = "see .agent/new-x for details"\n',  # prose, but not in a prose sink
        'p = root / agent_dir_name() / "new-x"\n',
        'p = Path(agent_dir_name(), "new-x")\n',
    ],
)
def test_any_spelling_of_the_folder_fails_the_ban(source: str):
    """Each way to spell the folder outside the helpers is refused statically."""
    assert agent_literals(source) != []


@pytest.mark.parametrize(
    "source",
    [
        '"""Records live in `.agent/verify/`."""\n',
        'p.add_argument("--x", help="advisory area locks under .agent/leases, one per area")\n',
        'raise AgentError(f"{path} is a link; export reads only a real .agent folder")\n',
        'ToolSpec(fn, "Lease", "Manage an advisory lock under .agent/leases. " + SESSION_ARG)\n',
        'x = ".agents/skills"\ny = "mcp_servers.agent"\nz = ".agent-pre-push-"\n',
        'ignore = f"{agent_dir_name()}/"\n',
    ],
)
def test_prose_sinks_and_other_names_pass_the_ban(source: str):
    assert agent_literals(source) == []


def test_agent_path_refuses_an_unregistered_subtree():
    with pytest.raises(ValueError, match="not registered"):
        core.agent_path(Path(), "new-x")
    with pytest.raises(ValueError, match="not registered"):
        core.agent_parts("new-x", "y")
    for name in (*core.AGENT_SUBTREES, *core.AGENT_UNREGISTERED):
        assert core.agent_path(Path("/r"), name, "y") == Path("/r") / ".agent" / name / "y"
    assert core.agent_root(Path("/r")) == Path("/r") / ".agent"
    assert str(core.agent_path(Path(), "sessions")) == str(Path(".agent") / "sessions")  # byte-equal
    assert core.agent_display("raw", "unassigned") == ".agent/raw/unassigned"


@pytest.mark.parametrize(
    "build",
    [
        # runtime fixtures: a component that leaves the subtree, through every helper.
        lambda root: core.agent_path(root, "sessions", "..", "new-x"),
        lambda root: root.joinpath(*core.agent_parts("sessions", "..", "new-x")),
        lambda root: core.agent_path(root, "sessions", "a/../../new-x"),
        lambda root: core.agent_path(root, "sessions", "/etc"),
        lambda root: core.agent_path(root, "sessions", "C:x"),
        lambda root: core.agent_root(root) / "new-x",
        lambda root: core.agent_root(root) / "sessions/../new-x",
        lambda root: core.agent_root(root).joinpath("new-x"),
        lambda root: core.agent_root(root) / "/etc",
        lambda root: core.agent_display("sessions", ".."),
    ],
)
def test_the_helpers_refuse_a_path_that_leaves_its_subtree(build: Any):
    with pytest.raises(ValueError):
        build(Path("/r"))


def test_agent_root_still_reads_and_appends_registered_names():
    root = core.agent_root(Path("/r"))
    assert root / "sessions" / "x" == Path("/r/.agent/sessions/x")
    assert root.parent == Path("/r") and type(root.parent) is type(Path())
    assert root.joinpath("raw", "a") == Path("/r/.agent/raw/a")


def test_the_session_backstop_finds_every_spelling():
    """runtime fixtures that the static ban cannot see (concatenation, `chr()`): the
    conftest backstop (`unregistered_agent_dirs`) finds what they create. Built outside this
    session's basetemp, so the backstop itself does not fail the run on them."""
    import tempfile

    from conftest import unregistered_agent_dirs

    builders = [
        lambda root: root / ("." + "agent") / "new-concat",
        lambda root: root / (chr(46) + "agent") / "new-chr",
        lambda root: root / "nested" / "".join([".", "agent"]) / "new-nested",
        lambda root: core.agent_path(root, "sessions", "registered-is-fine"),
    ]
    with tempfile.TemporaryDirectory(prefix="p9-backstop-") as directory:
        root = Path(directory)
        for build in builders:
            build(root).mkdir(parents=True)
        found = {p.name for p in unregistered_agent_dirs(root)}
    assert found == {"new-concat", "new-chr", "new-nested"}


def test_the_backstop_walks_registered_subtrees_links_and_loops():
    """Review fixtures: a nested repo's `.agent/` under `.agent/sessions/...` (reached through
    `agent_root(repo).resolve()`, a plain path), a `.agent` that is a link to a directory inside
    the scanned tree, and a link loop that must not recurse. Plus controls: a path derived
    from `agent_root`, and one smuggled through an exception message."""
    import tempfile

    from conftest import unregistered_agent_dirs

    with tempfile.TemporaryDirectory(prefix="p9-backstop-") as directory:
        root = Path(directory).resolve()
        repo = core.agent_path(root, "sessions", "nested-repo")
        (core.agent_root(repo).resolve() / "nested-x").mkdir(parents=True)
        target = root / "state-target"
        (target / "linked-x").mkdir(parents=True)
        (root / "linked-repo").mkdir()
        links.make(core.agent_root(root / "linked-repo"), target, is_dir=True)
        links.make(root / "loop", root, is_dir=True)
        (core.agent_root(root).parent / core.agent_root(root).name / "derived-x").mkdir(parents=True)
        scope: dict[str, Any] = {"root": root}
        exec('p = root / ValueError(".agent/sink-x").args[0]\np.mkdir(parents=True)\n', scope)
        found = {p.relative_to(root).as_posix() for p in unregistered_agent_dirs(root)}
    assert found == {
        ".agent/sessions/nested-repo/.agent/nested-x",
        "linked-repo/.agent/linked-x",
        ".agent/derived-x",
        ".agent/sink-x",
    }


def test_the_backstop_judges_every_entry_by_name():
    """Review fixtures: a link directly under `.agent` (to a directory elsewhere in the tree,
    then written through) is a new subtree by its own name, as is one inside a `.agent` that is
    itself a link; an unregistered file counts too. Registered names, files included, pass."""
    import tempfile

    from conftest import unregistered_agent_dirs

    with tempfile.TemporaryDirectory(prefix="p9-backstop-") as directory:
        root = Path(directory).resolve()
        state = core.agent_root(root).resolve()
        state.mkdir()
        storage = root / "storage"
        storage.mkdir()
        links.make(state / "new-linked-subtree", storage, is_dir=True)
        (state / "new-linked-subtree" / "record.txt").write_text("probe")
        (state / "new-file.json").write_text("{}")
        core.agent_path(root, "live.json").write_text("{}")
        core.agent_path(root, "sessions").mkdir()
        target = root / "state-target"
        target.mkdir()
        links.make(target / "inner-linked", storage, is_dir=True)
        (root / "linked-repo").mkdir()
        links.make(core.agent_root(root / "linked-repo"), target, is_dir=True)
        found = {p.relative_to(root).as_posix() for p in unregistered_agent_dirs(root)}
    assert found == {
        ".agent/new-linked-subtree",
        ".agent/new-file.json",
        "linked-repo/.agent/inner-linked",
    }


def test_the_registry_and_its_exceptions_do_not_overlap():
    assert not set(core.AGENT_SUBTREES) & core.AGENT_UNREGISTERED


# ---------------------------------------------------------------- selection


@pytest.mark.parametrize(
    "changed",
    [
        "src/agent_cli/export.py",
        "src/agent_cli/journal/block.py",
        "src/agent_cli/dispatch/state.py",
    ],
)
def test_both_checks_run_in_the_scoped_selection_of_any_source_change(changed: str):
    """A docstring, symbol or `.agent` path can change in any module, so
    the scoped selection (`test_select.choose`) of any source change carries both checks."""
    import tomllib

    from agent_cli.dispatch import test_select

    table = tomllib.loads((ROOT / "tests" / "selection.toml").read_text(encoding="utf-8"))
    available = sorted(f"tests/{p.name}" for p in (ROOT / "tests").glob("test_*.py"))
    seen = test_select.Observed(table, [("M", changed)], available, None, lambda _p: [], lambda _p: [])
    selected = test_select.choose(seen, table)
    assert {"tests/test_docs_refs.py", "tests/test_prose_rules.py"} <= set(selected.tests)


def test_every_rule_that_maps_source_carries_both_checks():
    """The structural half of the above: the fallback (a new, unmapped module) and every rule that
    maps a source module name both checks, so no module's change escapes them. A catch-all
    `src/**` rule would do it in one line but would make every new module count as mapped."""
    import tomllib
    from pathlib import PurePosixPath

    table = tomllib.loads((ROOT / "tests" / "selection.toml").read_text(encoding="utf-8"))
    checks = {"tests/test_docs_refs.py", "tests/test_prose_rules.py"}
    sources = [p.relative_to(ROOT).as_posix() for p in (ROOT / "src" / "agent_cli").rglob("*.py")]
    missing = [
        rule.get("name") or rule.get("reason")
        for kind in ("broad", "bundle")
        for rule in table[kind]
        if any(PurePosixPath(s).full_match(p) for s in sources for p in rule["paths"])
        and not checks <= set(rule["tests"])
    ]
    missing += ["fallback"] if not all(checks <= set(r["tests"]) for r in table["fallback"]) else []
    assert missing == []
