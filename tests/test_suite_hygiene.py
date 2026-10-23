"""Static hygiene of the test suite: an AST scan of `tests/` that starts no processes.

It refuses trivial asserts and duplicate test bodies outright, and four things unless
`tests/budget.toml` (`agent_cli.suite_budget`) allows them: a test with no assert and no
`pytest.raises`, or only mock-call asserts (`[weak]`); more `time.sleep` calls in a file than its
`[sleep]` count, absent meaning 0; and an xfail, an unconditional skip or a non-platform `skipif`
(`[[skip]]`, unexpired). Every skip and xfail needs a reason. An entry that is no longer needed
fails too, so the lists only shrink. A `[slow]` entry is needed while it names a case pytest
collects, as far as the source can tell; the per-test spawn ceiling that reads it is in `suite_cost.py`.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pytest

from agent_cli import suite_budget

TESTS = Path(__file__).resolve().parent
IGNORED = ("fixtures/format_compat_base",)  # frozen data, never collected (conftest.collect_ignore)

MOCK_ASSERTS = frozenset(
    {
        "assert_called",
        "assert_called_once",
        "assert_called_with",
        "assert_called_once_with",
        "assert_any_call",
        "assert_has_calls",
        "assert_not_called",
        "assert_awaited",
        "assert_awaited_once",
        "assert_awaited_with",
        "assert_awaited_once_with",
        "assert_any_await",
        "assert_has_awaits",
        "assert_not_awaited",
    }
)
MOCK_ATTRS = frozenset(
    {
        "called",
        "call_count",
        "call_args",
        "call_args_list",
        "mock_calls",
        "method_calls",
        "await_count",
        "await_args",
    }
)
#: Markers that only place a test on a worker; two bodies differing only in these are duplicates.
SCHEDULING_MARKS = frozenset({"xdist_group", "timing"})
NO_ENTRY = "no budget entry allows this"
#: The only files that may use `suite_cost.uncounted`: the shared record cache, the host-facts
#: fence and the plugin's own.
UNCOUNTED_USERS = frozenset({"land_helpers.py", "isolation.py", "suite_cost.py", "test_suite_cost_plugin.py"})


@dataclass
class Site:
    file: str
    line: int
    what: str
    reason: str = ""


@dataclass
class Found:
    """What the scan sees: the budgetable sites by budget key, and the problems no entry allows."""

    #: Each test node with its case ids (None when unreadable) and its argument names.
    tests: dict[str, tuple[set[str] | None, frozenset[str]]] = field(default_factory=dict)
    #: Fixture names defined with `params=`, and whether any file generates tests itself.
    param_fixtures: set[str] = field(default_factory=set)
    generates: bool = False
    weak: dict[str, Site] = field(default_factory=dict)
    sleep: dict[str, list[int]] = field(default_factory=dict)
    skip: dict[str, Site] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


def _files(tests: Path) -> Iterator[Path]:
    for path in sorted(tests.rglob("*.py")):
        rel = path.relative_to(tests).as_posix()
        if not any(rel.startswith(f"{ignored}/") for ignored in IGNORED):
            yield path


def _is_test_file(path: Path) -> bool:
    return path.name.startswith("test_")


def _dotted(node: ast.expr) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _owners(tree: ast.Module, rel: str) -> Iterator[tuple[str, ast.AST, ast.AST | None]]:
    """Each top-level piece of the file with the budget node it belongs to, and its test function."""
    for stmt in tree.body:
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            yield f"{rel}::{stmt.name}", stmt, stmt
        elif isinstance(stmt, ast.ClassDef):
            for deco in stmt.decorator_list:
                yield f"{rel}::{stmt.name}", deco, None
            for item in stmt.body:
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                    yield f"{rel}::{stmt.name}::{item.name}", item, item
                else:
                    yield f"{rel}::{stmt.name}", item, None
        elif isinstance(stmt, ast.Assign | ast.AnnAssign):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            name = names[0] if len(names) == 1 else "pytestmark"
            yield (rel if name == "pytestmark" else f"{rel}::{name}"), stmt, None
        else:
            yield rel, stmt, None


def _test_functions(
    tree: ast.Module, rel: str
) -> Iterator[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef, ast.ClassDef | None]]:
    for stmt in tree.body:
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef) and stmt.name.startswith("test"):
            yield f"{rel}::{stmt.name}", stmt, None
        elif isinstance(stmt, ast.ClassDef) and stmt.name.startswith("Test"):
            for item in stmt.body:
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef) and item.name.startswith("test"):
                    yield f"{rel}::{stmt.name}::{item.name}", item, stmt


def _uses_uncounted(node: ast.AST) -> bool:
    if isinstance(node, ast.ImportFrom):
        return any(a.name in ("uncounted", "*") for a in node.names) and (node.module or "").endswith(
            "suite_cost"
        )
    if isinstance(node, ast.Attribute):
        return node.attr == "uncounted"
    if isinstance(node, ast.Call) and _dotted(node.func) == "getattr":
        return any(isinstance(a, ast.Constant) and a.value == "uncounted" for a in node.args)
    return isinstance(node, ast.Name) and node.id == "uncounted"


def _truthy_literal(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return bool(node.value)
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        return bool(node.elts)
    if isinstance(node, ast.Dict):
        return bool(node.keys)
    if isinstance(node, ast.Compare):
        return all(isinstance(n, ast.Constant) for n in (node.left, *node.comparators))
    return False


def _runs(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> Iterator[ast.AST]:
    """The nodes the test body runs: a nested function or lambda only when the body names it."""
    nested: dict[str, list[ast.AST]] = {}
    seen: set[str] = set()
    todo: list[ast.AST] = list(fn.body)
    while todo:
        node = todo.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            if node.name in seen:
                todo += node.body
            else:
                nested.setdefault(node.name, []).extend(node.body)
            continue
        if isinstance(node, ast.Lambda):
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id not in seen:
            seen.add(node.id)
            todo += nested.pop(node.id, [])
        yield node
        todo += ast.iter_child_nodes(node)


def _weakness(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """Why the test checks nothing real, or None when it asserts or expects a raise."""
    mock = False
    for node in _runs(fn):
        if isinstance(node, ast.Assert):
            if not any(isinstance(n, ast.Attribute) and n.attr in MOCK_ATTRS for n in ast.walk(node.test)):
                return None
            mock = True
        elif isinstance(node, ast.Attribute | ast.Name) and _dotted(node) in ("pytest.raises", "raises"):
            return None
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in MOCK_ASSERTS
        ):
            mock = True
    return "has only mock-call asserts" if mock else "has no assert and no pytest.raises"


def _body_key(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    body = fn.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]  # the docstring
    decorators = [d for d in fn.decorator_list if _mark_name(d) not in SCHEDULING_MARKS]
    return ast.dump(ast.Module(body=[*decorators, fn.args, *body], type_ignores=[]))  # type: ignore[list-item]


def _mark_name(node: ast.expr) -> str:
    dotted = _dotted(node.func if isinstance(node, ast.Call) else node)
    return dotted.removeprefix("pytest.mark.") if dotted.startswith("pytest.mark.") else ""


class _Unknown(Exception):
    """The case ids cannot be read from the source."""


def _value_id(node: ast.expr, argname: str, index: int) -> str:
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
    ):
        return f"-{node.operand.value}"
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str | bytes):
            text = node.value.decode("ascii", "replace") if isinstance(node.value, bytes) else node.value
            if not (text.isascii() and (text.isprintable() or not text)) or "�" in text:
                raise _Unknown  # pytest escapes these
            return text
        if node.value is None or isinstance(node.value, bool | int | float):
            return str(node.value)
    if isinstance(node, ast.Name | ast.Attribute):
        raise _Unknown  # an enum, class or function names itself
    return f"{argname}{index}"


def _parametrize_ids(call: ast.Call) -> list[str]:
    if len(call.args) < 2 or not isinstance(call.args[1], ast.List | ast.Tuple):
        raise _Unknown
    names_node = call.args[0]
    if isinstance(names_node, ast.Constant) and isinstance(names_node.value, str):
        names = [n.strip() for n in names_node.value.split(",") if n.strip()]
    elif isinstance(names_node, ast.List | ast.Tuple) and all(
        isinstance(e, ast.Constant) for e in names_node.elts
    ):
        names = [str(e.value) for e in names_node.elts]  # type: ignore[attr-defined]
    else:
        raise _Unknown
    given: list[str | None] = []
    for kw in call.keywords:
        if kw.arg == "ids":
            if not isinstance(kw.value, ast.List | ast.Tuple):
                raise _Unknown
            for e in kw.value.elts:
                if not isinstance(e, ast.Constant):
                    raise _Unknown
                given.append(None if e.value is None else str(e.value))
    ids = []
    for i, element in enumerate(call.args[1].elts):
        if isinstance(element, ast.Call) and _dotted(element.func) == "pytest.param":
            explicit = next((k.value for k in element.keywords if k.arg == "id"), None)
            if explicit is not None:
                if not isinstance(explicit, ast.Constant):
                    raise _Unknown
                ids.append(str(explicit.value))
                continue
            values = element.args
        elif len(names) == 1:
            values = [element]
        elif isinstance(element, ast.Tuple | ast.List):
            values = element.elts
        else:
            raise _Unknown
        if i < len(given) and given[i] is not None:
            ids.append(str(given[i]))
        else:
            ids.append("-".join(_value_id(v, n, i) for v, n in zip(values, names, strict=True)))
    if len(set(ids)) != len(ids):
        raise _Unknown  # pytest numbers duplicate ids
    return ids


def _case_ids(fn: ast.FunctionDef | ast.AsyncFunctionDef, cls: ast.ClassDef | None) -> set[str] | None:
    """The ids pytest gives the test's cases, `{""}` when it has none, None when the source cannot say."""
    if cls is not None and any(_mark_name(d) == "parametrize" for d in cls.decorator_list):
        return None
    ids: list[str] | None = None
    try:
        for deco in reversed(fn.decorator_list):
            if _mark_name(deco) == "parametrize":
                if not isinstance(deco, ast.Call):
                    return None
                cases = _parametrize_ids(deco)
                ids = cases if ids is None else [f"{a}-{b}" for a in ids for b in cases]
    except _Unknown:
        return None
    return {""} if ids is None else set(ids)


def _platform_attr(node: ast.expr) -> bool:
    dotted = _dotted(node)
    return dotted == "os.name" or dotted == "sys.platform" or dotted.endswith(".sys.platform")


def _platform_condition(node: ast.expr, consts: dict[str, ast.expr]) -> bool:
    """True when the condition reads `sys.platform` or `os.name` and nothing else but literals."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            node = ast.parse(node.value, mode="eval").body
        except SyntaxError:
            return False
    return _platform_only(node, consts, 1) and _reads_platform(node, consts, 1)


def _reads_platform(node: ast.expr, consts: dict[str, ast.expr], depth: int) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and _platform_attr(n):
            return True
        if isinstance(n, ast.Name) and (value := consts.get(n.id)) is not None and depth < 4:
            if _reads_platform(value, consts, depth + 1):
                return True
    return False


def _platform_only(node: ast.expr, consts: dict[str, ast.expr], depth: int) -> bool:
    """True when the condition reads nothing but `sys.platform`, `os.name` and literals."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.Attribute):
        return _platform_attr(node)
    if isinstance(node, ast.Name):
        value = consts.get(node.id)
        return value is not None and depth < 4 and _platform_only(value, consts, depth + 1)
    if isinstance(node, ast.BoolOp):
        return all(_platform_only(v, consts, depth + 1) for v in node.values)
    if isinstance(node, ast.UnaryOp):
        return _platform_only(node.operand, consts, depth + 1)
    if isinstance(node, ast.Compare):
        return all(_platform_only(n, consts, depth + 1) for n in (node.left, *node.comparators))
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        return all(_platform_only(n, consts, depth + 1) for n in node.elts)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "startswith":
        return _platform_attr(node.func.value) and all(isinstance(a, ast.Constant) for a in node.args)
    return False


def _reason(call: ast.Call | None, positional: bool) -> str:
    if call is None:
        return ""
    for kw in call.keywords:
        if kw.arg in ("reason", "msg"):
            return _text(kw.value)
    return _text(call.args[0]) if positional and call.args else ""


def _text(node: ast.expr) -> str:
    if isinstance(node, ast.Constant):
        return str(node.value).strip()
    return ast.unparse(node)


def _always(stmts: list[ast.stmt]) -> Iterator[ast.expr]:
    """The expression statements that run whenever `stmts` do: no branch, handler or loop guard."""
    for s in stmts:
        if isinstance(s, ast.Expr):
            yield s.value
        elif isinstance(s, ast.If | ast.While) and isinstance(s.test, ast.Constant):
            yield from _always(s.body if s.test.value else s.orelse)
        elif isinstance(s, ast.With | ast.AsyncWith):
            yield from _always(s.body)
        elif isinstance(s, ast.Try | ast.TryStar):
            yield from _always(s.body + s.finalbody)


def _skips(owner: str, part: ast.AST, rel: str, consts: dict[str, ast.expr], found: Found) -> None:
    called = {id(n.func) for n in ast.walk(part) if isinstance(n, ast.Call)}
    body = part.body if isinstance(part, ast.FunctionDef | ast.AsyncFunctionDef) else [part]
    unconditional = {id(e) for e in _always([s for s in body if isinstance(s, ast.stmt)])}
    for node in ast.walk(part):
        call = node if isinstance(node, ast.Call) else None
        target = call.func if call is not None else node
        if not isinstance(target, ast.Attribute) or (call is None and id(node) in called):
            continue
        name = _dotted(target)
        where = f"{rel}:{node.lineno}"  # type: ignore[attr-defined]
        if name in ("pytest.mark.skip", "pytest.mark.xfail", "pytest.mark.skipif"):
            kind = name.rsplit(".", 1)[1]
            reason = _reason(call, positional=kind == "skip")
            needs = kind != "skipif"
            if kind == "skipif":
                cond = call.args[0] if call is not None and call.args else None
                needs = cond is None or not _platform_condition(cond, consts)
        elif call is not None and name in ("pytest.skip", "pytest.xfail", "pytest.importorskip"):
            kind = name.split(".", 1)[1]
            reason = _reason(call, positional=kind != "importorskip")
            needs = kind == "xfail" or (kind == "skip" and id(node) in unconditional)
        else:
            continue
        if not reason:
            found.problems.append(f"skip-reason: {where} {kind} has no reason; {NO_ENTRY}")
        if needs:
            label = {"skipif": "non-platform skipif", "skip": "unconditional skip"}.get(kind, kind)
            found.skip.setdefault(owner, Site(rel, node.lineno, label, reason))  # type: ignore[attr-defined]


def scan(tests: Path) -> Found:
    """Scan every Python file under `tests`; node ids are relative to its parent, like pytest's."""
    found = Found()
    first_body: dict[str, str] = {}
    for path in _files(tests):
        rel = path.relative_to(tests.parent).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        sleep_names = {"time.sleep"}
        exempt = path.parent == tests and path.name in UNCOUNTED_USERS
        for node in ast.walk(tree):
            if not exempt and _uses_uncounted(node):
                found.problems.append(
                    f"uncounted-use: {rel}:{getattr(node, 'lineno', 0)} uses suite_cost.uncounted, which only the shared "
                    f"record cache in tests/land_helpers.py (who pays depends on scheduling) and the host-facts "
                    f"fence in tests/isolation.py (what the process tree costs depends on how pytest was launched) "
                    f"may use to leave starts out of a test's ceiling; {NO_ENTRY}"
                )
            if isinstance(node, ast.ImportFrom) and node.module == "time":
                sleep_names |= {a.asname or a.name for a in node.names if a.name == "sleep"}
            elif isinstance(node, ast.Import):
                sleep_names |= {f"{a.asname}.sleep" for a in node.names if a.name == "time" and a.asname}
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                found.generates |= node.name == "pytest_generate_tests"
                for deco in node.decorator_list:
                    if isinstance(deco, ast.Call) and _dotted(deco.func) == "pytest.fixture":
                        kws = {k.arg: k.value for k in deco.keywords}
                        if "params" in kws:
                            alias = kws.get("name")
                            found.param_fixtures.add(
                                str(alias.value) if isinstance(alias, ast.Constant) else node.name
                            )
            if isinstance(node, ast.Assert) and _truthy_literal(node.test):
                found.problems.append(
                    f"trivial-assert: {rel}:{node.lineno} asserts a literal, which always passes; {NO_ENTRY}"
                )
        sleeps = sorted(
            n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call) and _dotted(n.func) in sleep_names
        )
        if sleeps:
            found.sleep[rel] = sleeps
        consts = {
            t.id: s.value
            for s in tree.body
            if isinstance(s, ast.Assign)
            for t in s.targets
            if isinstance(t, ast.Name)
        }
        for owner, part, _ in _owners(tree, rel):
            _skips(owner, part, rel, consts, found)
        if not _is_test_file(path):
            continue
        for node_id, fn, cls in _test_functions(tree, rel):
            args = {a.arg for a in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs)}
            found.tests[node_id] = (_case_ids(fn, cls), frozenset(args))
            if (why := _weakness(fn)) is not None:
                found.weak[node_id] = Site(rel, fn.lineno, why)
            key = _body_key(fn)
            if (
                first := first_body.setdefault(key, f"{rel}:{fn.lineno} {fn.name}")
            ) != f"{rel}:{fn.lineno} {fn.name}":
                found.problems.append(
                    f"duplicate-test: {rel}:{fn.lineno} {fn.name} has the same body as {first}; {NO_ENTRY}"
                )
    return found


def _entry_line(text: str, table: str, key: str) -> int:
    header, quoted, inside = f"[{table}]", f'"{key}"', False
    for i, line in enumerate(text.splitlines(), 1):
        if line.startswith("["):
            inside = line.strip() == header
        elif inside and quoted in line:
            return i
    return 0


def _slow_problem(found: Found, key: str) -> str | None:
    """Why a `[slow]` key names no collectable case, or None when it does or the source cannot say."""
    match = re.fullmatch(r"(?P<base>[^\[@]+)(?:\[(?P<case>.*)\])?(?:@[^\[\]]*)?", key)
    if match is None or match["base"] not in found.tests:
        return "its test no longer exists"
    ids, args = found.tests[match["base"]]
    if ids is None or found.generates or args & found.param_fixtures:
        return None
    case = match["case"] or ""
    if case in ids:
        return None
    return f"the test has no case [{case}]" if case else "the test is parametrized; name one of its cases"


def check(found: Found, budget: suite_budget.Budget, text: str, today: date) -> list[str]:
    """Every hygiene failure, each naming its guard, file and line, and the entry that would allow it."""
    out = list(found.problems)
    for node, site in sorted(found.weak.items()):
        if node not in budget.weak:
            out.append(
                f'weak-test: {site.file}:{site.line} {node} {site.what}; allowed only by [weak] "{node}"'
            )
    for rel, lines in sorted(found.sleep.items()):
        allowed = int(budget.sleep.get(rel, 0))
        if len(lines) > allowed:
            out.append(
                f"sleep-ratchet: {rel}:{lines[allowed]} has {len(lines)} time.sleep calls, over its "
                f'[sleep] "{rel}" = {allowed}'
            )
    for node, site in sorted(found.skip.items()):
        expires = budget.skip.get(node)
        if expires is None or expires < today:
            state = "has none" if expires is None else f"expired {expires}"
            out.append(
                f"skip-entry: {site.file}:{site.line} {site.what} on {node} needs an unexpired [[skip]] entry "
                f'node = "{node}" ({state})'
            )
    stale: list[tuple[str, str, str]] = []
    stale += [("slow", key, why) for key in budget.slow if (why := _slow_problem(found, key)) is not None]
    for rel, allowed in budget.sleep.items():
        count = len(found.sleep.get(rel, []))
        if count < allowed:
            stale.append(("sleep", rel, f"the file has {count} time.sleep calls now; lower it to {count}"))
    stale += [
        ("weak", node, "the test asserts now or is gone") for node in budget.weak if node not in found.weak
    ]
    stale += [
        ("[skip]", node, "nothing there skips or xfails now")
        for node in budget.skip
        if node not in found.skip
    ]
    for table, key, why in sorted(stale):
        out.append(
            f'stale-entry: {suite_budget.PATH}:{_entry_line(text, table, key)} [{table}] "{key}" is no longer '
            f"needed ({why}); remove it"
        )
    return out


def test_the_suite_passes_its_hygiene_scan():
    text = (TESTS / "budget.toml").read_text(encoding="utf-8")
    problems = check(scan(TESTS), suite_budget.parse(text), text, date.today())
    assert not problems, "\n".join(problems)


SUITE = "[suite]\ntests_max = 1\nspawns_max = 1\nheadroom_pct = 1\n"


def _run(
    tmp_path: Path, files: dict[str, str], budget: str = "", today: date = date(2026, 10, 22)
) -> list[str]:
    tests = tmp_path / "tests"
    tests.mkdir(exist_ok=True)
    for name, source in files.items():
        (tests / name).write_text(source, encoding="utf-8")
    text = SUITE + budget
    return check(scan(tests), suite_budget.parse(text), text, today)


@pytest.mark.parametrize(
    ("source", "guard"),
    [
        ("def test_a():\n    run()\n", "weak-test: tests/test_x.py:1 tests/test_x.py::test_a has no assert"),
        (
            "def test_a(m):\n    m.assert_called_once()\n",
            "weak-test: tests/test_x.py:1 tests/test_x.py::test_a has only mock",
        ),
        ("def test_a(m):\n    run()\n    assert m.call_count == 1\n", "has only mock-call asserts"),
        ("def test_a():\n    assert True\n", "trivial-assert: tests/test_x.py:2 asserts a literal"),
        ("def test_a(x):\n    assert (x, 'oops')\n", "trivial-assert: tests/test_x.py:2"),
        ("def test_a():\n    assert 1 == 1\n", "trivial-assert: tests/test_x.py:2"),
        (
            "def test_a(x):\n    assert x == 2\n\ndef test_b(x):\n    '''Doc.'''\n    assert x == 2\n",
            "duplicate-test: tests/test_x.py:4 test_b has the same body as tests/test_x.py:1 test_a",
        ),
        (
            "import time\ndef test_a():\n    time.sleep(1)\n    assert f()\n",
            'sleep-ratchet: tests/test_x.py:3 has 1 time.sleep calls, over its [sleep] "tests/test_x.py" = 0',
        ),
        (
            "import pytest\n@pytest.mark.skip\ndef test_a():\n    assert f()\n",
            "skip-reason: tests/test_x.py:2 skip has no reason",
        ),
        (
            "import pytest, sys\n@pytest.mark.skipif(sys.platform == 'win32')\ndef test_a():\n    assert f()\n",
            "skip-reason: tests/test_x.py:2 skipif",
        ),
        (
            "import pytest\ndef test_a():\n    if f():\n        pytest.skip()\n    assert f()\n",
            "skip-reason: tests/test_x.py:4 skip",
        ),
        (
            "import pytest\n@pytest.mark.xfail(reason='flaky')\ndef test_a():\n    assert f()\n",
            'skip-entry: tests/test_x.py:2 xfail on tests/test_x.py::test_a needs an unexpired [[skip]] entry node = "tests/test_x.py::test_a" (has none)',
        ),
        (
            "import pytest\ndef test_a():\n    pytest.skip('later')\n    assert f()\n",
            "skip-entry: tests/test_x.py:3 unconditional skip",
        ),
        (
            "import pytest, shutil\nNODE = pytest.mark.skipif(shutil.which('node') is None, reason='no node')\n",
            "skip-entry: tests/test_x.py:2 non-platform skipif on tests/test_x.py::NODE needs",
        ),
        (
            "import pytest, os\npytestmark = pytest.mark.skipif(os.name == 'nt' or os.geteuid() == 0, reason='posix')\n",
            'non-platform skipif on tests/test_x.py needs an unexpired [[skip]] entry node = "tests/test_x.py"',
        ),
        (
            "import pytest\npytest.skip('disabled', allow_module_level=True)\n",
            'skip-entry: tests/test_x.py:2 unconditional skip on tests/test_x.py needs an unexpired [[skip]] entry node = "tests/test_x.py"',
        ),
        (
            "import pytest\ndef test_a():\n    if True:\n        pytest.skip('disabled')\n    assert f()\n",
            "skip-entry: tests/test_x.py:4 unconditional skip on tests/test_x.py::test_a",
        ),
        (
            "import pytest\ndef test_a():\n    with f():\n        pytest.skip('disabled')\n",
            "skip-entry: tests/test_x.py:4 unconditional skip on tests/test_x.py::test_a",
        ),
        (
            "import pytest\n@pytest.mark.skipif(True, reason='disabled')\ndef test_a():\n    assert f()\n",
            "skip-entry: tests/test_x.py:2 non-platform skipif on tests/test_x.py::test_a",
        ),
        (
            "import pytest\n@pytest.mark.skipif('1 == 1', reason='disabled')\ndef test_a():\n    assert f()\n",
            "skip-entry: tests/test_x.py:2 non-platform skipif on tests/test_x.py::test_a",
        ),
        (
            "import pytest\n"
            "@pytest.mark.xdist_group('timing-0')\ndef test_a(x):\n    assert x == 2\n\n"
            "@pytest.mark.timing\n@pytest.mark.xdist_group('timing-1')\ndef test_b(x):\n    assert x == 2\n",
            "duplicate-test: tests/test_x.py:8 test_b has the same body as tests/test_x.py:3 test_a",
        ),
        (
            "import suite_cost\ndef test_a():\n    with suite_cost.uncounted():\n        run()\n    assert f()\n",
            "uncounted-use: tests/test_x.py:3 uses suite_cost.uncounted, which only the shared record cache in "
            "tests/land_helpers.py (who pays depends on scheduling) and the host-facts fence in tests/isolation.py",
        ),
        ("from suite_cost import uncounted\n", "uncounted-use: tests/test_x.py:1 uses suite_cost.uncounted"),
        (
            "import suite_cost\nset_aside = getattr(suite_cost, 'uncounted')\n",
            "uncounted-use: tests/test_x.py:2 uses suite_cost.uncounted",
        ),
        (
            "import time as clock\ndef test_a():\n    clock.sleep(10)\n    assert f()\n",
            'sleep-ratchet: tests/test_x.py:3 has 1 time.sleep calls, over its [sleep] "tests/test_x.py" = 0',
        ),
        (
            "def test_a():\n    def check(x):\n        assert x\n    run()\n",
            "weak-test: tests/test_x.py:1 tests/test_x.py::test_a has no assert",
        ),
    ],
)
def test_each_rule_refuses_its_violation_and_names_its_guard(tmp_path: Path, source: str, guard: str):
    problems = _run(tmp_path, {"test_x.py": source})
    assert any(guard in p for p in problems), problems


def test_platform_skips_need_only_a_reason(tmp_path: Path):
    source = (
        "import os, sys, pytest\n"
        "WINDOWS = sys.platform == 'win32'\n"
        "posix = pytest.mark.skipif(os.name == 'nt', reason='posix')\n"
        "@pytest.mark.skipif(WINDOWS, reason='posix only')\n"
        "def test_a():\n    assert f()\n"
        "@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='linux')\n"
        "def test_b():\n    assert g()\n"
        "def test_c():\n    if os.name == 'nt':\n        pytest.skip('posix')\n    assert h()\n"
    )
    assert _run(tmp_path, {"test_x.py": source}) == []


def test_only_the_record_cache_the_host_fence_and_the_plugin_may_use_uncounted(tmp_path: Path):
    use = "import suite_cost\ndef helper():\n    with suite_cost.uncounted():\n        run()\n"
    (tmp_path / "tests" / "sub").mkdir(parents=True)
    problems = _run(
        tmp_path,
        {
            "land_helpers.py": use,
            "isolation.py": use,
            "suite_cost.py": use,
            "test_suite_cost_plugin.py": use,
            "sub/land_helpers.py": use,
            "sub/isolation.py": use,
        },
    )
    assert sorted(p.split(" uses")[0] for p in problems) == [
        "uncounted-use: tests/sub/isolation.py:3",
        "uncounted-use: tests/sub/land_helpers.py:3",
    ], problems


def test_a_nested_helper_the_test_calls_counts_as_its_assert(tmp_path: Path):
    source = (
        "def test_a():\n    def check(x):\n        assert x\n    check(run())\n"
        "def test_b():\n    def check(x):\n        assert x\n    run(callback=check)\n"
    )
    assert _run(tmp_path, {"test_x.py": source}) == []


def test_budget_entries_allow_what_they_name_until_they_expire(tmp_path: Path):
    files = {
        "test_x.py": (
            "import time, pytest\n"
            "def test_a():\n    run()\n"
            "@pytest.mark.xfail(reason='flaky')\n"
            "def test_b():\n    time.sleep(1)\n    assert f()\n"
        )
    }
    budget = (
        '[sleep]\n"tests/test_x.py" = 1\n[weak]\n"tests/test_x.py::test_a" = "seeded"\n'
        '[[skip]]\nnode = "tests/test_x.py::test_b"\nreason = "flaky"\nexpires = 2027-01-22\n'
    )
    assert _run(tmp_path, files, budget) == []
    (expired,) = _run(tmp_path, files, budget, today=date(2027, 1, 23))
    assert expired.startswith("skip-entry: tests/test_x.py:4 xfail") and "(expired 2027-01-22)" in expired


def test_an_entry_no_longer_needed_is_stale(tmp_path: Path):
    files = {"test_x.py": "import time\ndef test_a():\n    time.sleep(1)\n    assert f()\n"}
    budget = (
        '[slow]\n"tests/test_x.py::test_a[1]@timing-0" = 150\n"tests/test_x.py::test_gone" = 120\n'
        '[sleep]\n"tests/test_x.py" = 2\n"tests/test_old.py" = 1\n[weak]\n"tests/test_x.py::test_a" = "seeded"\n'
        '[[skip]]\nnode = "tests/test_x.py::test_a"\nreason = "flaky"\nexpires = 2027-01-22\n'
    )
    problems = _run(tmp_path, files, budget)
    assert [p.split(" is no longer")[0] for p in problems] == [
        'stale-entry: tests/budget.toml:14 [[skip]] "tests/test_x.py::test_a"',
        'stale-entry: tests/budget.toml:10 [sleep] "tests/test_old.py"',
        'stale-entry: tests/budget.toml:9 [sleep] "tests/test_x.py"',
        'stale-entry: tests/budget.toml:6 [slow] "tests/test_x.py::test_a[1]@timing-0"',
        'stale-entry: tests/budget.toml:7 [slow] "tests/test_x.py::test_gone"',
        'stale-entry: tests/budget.toml:12 [weak] "tests/test_x.py::test_a"',
    ]
    assert "lower it to 1" in problems[2]


def test_a_slow_entry_must_name_a_case_pytest_collects(tmp_path: Path):
    files = {
        "test_x.py": (
            "import pytest\n"
            "def test_plain():\n    assert f()\n"
            "@pytest.mark.parametrize('a', [1, 'x', None])\n"
            "@pytest.mark.parametrize(('b', 'c'), [(True, -2), pytest.param(3, 4, id='named')])\n"
            "def test_cases(a, b, c):\n    assert f()\n"
            "@pytest.mark.parametrize('a', [object()], ids=['given'])\n"
            "def test_given(a):\n    assert f()\n"
            "@pytest.mark.parametrize('a', VALUES)\n"
            "def test_unreadable(a):\n    assert f()\n"
            "def test_fixture_cases(mode):\n    assert f()\n"
        ),
        "conftest.py": "import pytest\n@pytest.fixture(params=['a', 'b'])\ndef mode(request):\n    return request.param\n",
    }
    kept = [
        "test_plain@timing-0",
        "test_cases[True--2-1]",
        "test_cases[named-x]@timing-1",
        "test_cases[True--2-None]",
        "test_given[given]",
        "test_unreadable[anything]",
        "test_fixture_cases[a]",
    ]
    stale = ["test_plain[deleted]@timing-0", "test_cases[True--2-2]", "test_cases", "test_given[a0]"]
    budget = "[slow]\n" + "".join(f'"tests/test_x.py::{key}" = 150\n' for key in kept + stale)
    problems = _run(tmp_path, files, budget)
    assert sorted(p.split('"')[1].split("::")[1] for p in problems) == sorted(stale), problems
    assert any("the test has no case [deleted]" in p for p in problems)
    assert any("the test is parametrized; name one of its cases" in p for p in problems)


def test_slow_case_ids_follow_pytest_for_bytes_empty_strings_and_escapes(tmp_path: Path):
    # Each kept id is what pytest 9 collects for the source; escaped values are unpredictable here.
    files = {
        "test_x.py": (
            "import pytest\n"
            "@pytest.mark.parametrize('a', [b'abc', ''])\n"
            "def test_bytes(a):\n    assert f()\n"
            "@pytest.mark.parametrize('a', [1])\n"
            "@pytest.mark.parametrize('b', [''])\n"
            "def test_stacked(a, b):\n    assert f()\n"
            "@pytest.mark.parametrize('a', ['t\\n'])\n"
            "def test_escaped(a):\n    assert f()\n"
        )
    }
    kept = ["test_bytes[abc]", "test_bytes[]", "test_stacked[-1]", "test_escaped[anything]"]
    stale = ["test_bytes[a0]", "test_stacked[1]"]
    budget = "[slow]\n" + "".join(f'"tests/test_x.py::{key}" = 150\n' for key in kept + stale)
    problems = _run(tmp_path, files, budget)
    assert sorted(p.split('"')[1].split("::")[1] for p in problems) == sorted(stale), problems
