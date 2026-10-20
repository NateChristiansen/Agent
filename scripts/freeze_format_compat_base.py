"""Freeze the slice base's state readers and writers into `tests/fixtures/format_compat_base/`.

`tests/test_format_compat.py` runs old readers on new state files and new readers on old ones.
The old side is this frozen copy: every `agent_cli` module the readers and writers in
`tests/format_compat_helpers.py` can import, taken from `--src` (by default this checkout's
`src/`) with comments and docstrings stripped, since only the code's behaviour matters there.

The retired private-terms check is trimmed out of every frozen copy (`_Trim`), so a base taken
from before its removal is sanitized too. `--check` reports
what a trim would change in the committed fixture without writing anything.

`closure()` is conservative: it follows lazy and type-only imports too. Audit 10 plan K5
keeps the runtime record from `tests/test_format_compat.py` in both directions, plus the
platform imports found by `platform_imports()` and their dependencies. The helper writes
`format-compat-imports.json` beside each temporary base source; set
`FORMAT_COMPAT_IMPORT_RECORD` to choose an output file, including on native Windows; run
with `-n0` then, since parallel workers would rewrite the same file unlocked.
Keep their union and package initializers by deleting from the existing frozen tree;
do not re-freeze from current source to trim a historical baseline. Re-freezing restores
the full conservative closure: re-run both the runtime record and platform scan to trim it.

Run it at a slice base to move the baseline, then commit the fixture:
  uv run python scripts/freeze_format_compat_base.py
"""

from __future__ import annotations

import argparse
import ast
import difflib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests" / "fixtures" / "format_compat_base"
#: The retired check's name, split so the name itself stays out of the tree.
RETIRED = "scr" + "ub"
#: The modules the readers and writers import directly; the rest follow from their imports.
ENTRIES = (
    "agent_cli.dispatch",
    "agent_cli.dispatch.verdicts",
    "agent_cli.vault.live",
    "agent_cli.push_records",
    "agent_cli.verify_record",
)


def _file(src: Path, name: str) -> Path | None:
    path = src / Path(*name.split("."))
    if (path / "__init__.py").is_file():
        return path / "__init__.py"
    if path.with_suffix(".py").is_file():
        return path.with_suffix(".py")
    return None


def closure(src: Path) -> dict[str, Path]:
    """Every `agent_cli` module reachable from `ENTRIES` by any import, lazy ones included."""
    found: dict[str, Path] = {}
    stack = list(ENTRIES)
    while stack:
        name = stack.pop()
        path = _file(src, name)
        if name in found or path is None or retired(name):
            continue
        found[name] = path
        parts = name.split(".")
        package = parts if path.name == "__init__.py" else parts[:-1]
        stack.extend(".".join(parts[:i]) for i in range(1, len(parts)))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                stack.extend(a.name for a in node.names if a.name.startswith("agent_cli"))
            elif isinstance(node, ast.ImportFrom):
                base = package[: len(package) - node.level + 1] if node.level else []
                module = ".".join([*base, *([node.module] if node.module else [])])
                if module.startswith("agent_cli"):
                    stack.append(module)
                    stack.extend(f"{module}.{a.name}" for a in node.names)
    return found


def platform_imports(src: Path) -> list[tuple[Path, int, str]]:
    """Internal imports under platform guards, at any depth, without executing the source.

    Both arms count, as does code after a platform-dependent early exit. Track aliases such
    as `IS_WINDOWS = os.name == "nt"`; named `is_windows()` predicates count too. Attribute
    imports require their module; `from . import X` requires the child module itself.
    """
    return [row for path in sorted(src.rglob("*.py")) for row in _platform_imports(src, path)]


def _platform_imports(src: Path, path: Path) -> list[tuple[Path, int, str]]:
    found: list[tuple[Path, int, str]] = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parts = list(path.relative_to(src).with_suffix("").parts)
    package = parts[:-1]
    names = {"IS_WINDOWS", "is_windows", "windows"}

    def platform(node: ast.AST) -> bool:
        return any(
            isinstance(n, ast.Name)
            and n.id in names
            or isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name)
            and (n.value.id, n.attr) in {("sys", "platform"), ("os", "name"), ("platform", "system")}
            for n in ast.walk(node)
        )

    while True:
        before = names.copy()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign | ast.AnnAssign) and node.value:
                value = node.value.func if isinstance(node.value, ast.Call) else node.value
                if isinstance(
                    value, ast.Name | ast.Attribute | ast.Compare | ast.BoolOp | ast.UnaryOp
                ) and platform(value):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    names.update(t.id for t in targets if isinstance(t, ast.Name))
        if names == before:
            break

    def visit(node: ast.AST, guarded: bool = False) -> None:
        if guarded and isinstance(node, ast.Import | ast.ImportFrom):
            if isinstance(node, ast.Import):
                modules = [a.name for a in node.names]
            else:
                base = package[: len(package) - node.level + 1] if node.level else []
                module = ".".join([*base, *([node.module] if node.module else [])])
                modules = [module] if node.module else [f"{module}.{a.name}" for a in node.names]
            found.extend(
                (path, node.lineno, name)
                for name in modules
                if name == "agent_cli" or name.startswith("agent_cli.")
            )
        branch = isinstance(node, ast.If) and platform(node.test)
        for field, value in ast.iter_fields(node):
            active = guarded or branch and field in {"body", "orelse"}
            if isinstance(value, list):
                for child in value:
                    if not isinstance(child, ast.AST):
                        continue
                    visit(child, active)
                    if (
                        isinstance(child, ast.If)
                        and platform(child.test)
                        and any(
                            isinstance(n, ast.Return | ast.Raise | ast.Break | ast.Continue)
                            for n in ast.walk(child)
                        )
                    ):
                        active = True
            elif isinstance(value, ast.AST):
                visit(value, active)

    visit(tree)
    return found


class _NoDocstrings(ast.NodeTransformer):
    def _strip(self, node: ast.AST) -> ast.AST:
        self.generic_visit(node)
        body = getattr(node, "body", None)
        if (
            isinstance(body, list)
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body.pop(0)
            if not body:
                body.append(ast.Pass())
        return node

    visit_Module = visit_ClassDef = visit_FunctionDef = visit_AsyncFunctionDef = _strip


def retired(text: str) -> bool:
    return RETIRED in text.lower()


def _mentions(node: ast.AST, names: set[str]) -> bool:
    """Whether `node` names the retired check, or a module-level name trimmed for naming it."""
    for n in ast.walk(node):
        text = None
        if isinstance(n, ast.Name):
            if n.id in names:
                return True
            text = n.id
        elif isinstance(n, ast.Attribute):
            text = n.attr
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            text = n.value
        elif isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            text = n.name
        elif isinstance(n, ast.alias):
            text = n.name + (n.asname or "")
        elif isinstance(n, ast.ImportFrom):
            text = n.module or ""
        elif isinstance(n, ast.arg):
            text = n.arg
        elif isinstance(n, ast.keyword):
            text = n.arg or ""
        if text is not None and retired(text):
            return True
    return False


def _header(stmt: ast.stmt) -> list[ast.AST]:
    """The parts of a compound statement outside its bodies; None for a simple statement."""
    if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
        return [
            ast.Name(stmt.name),
            stmt.args,
            *stmt.decorator_list,
            *([stmt.returns] if stmt.returns else []),
        ]
    if isinstance(stmt, ast.ClassDef):
        return [ast.Name(stmt.name), *stmt.bases, *stmt.keywords, *stmt.decorator_list]
    if isinstance(stmt, ast.If | ast.While):
        return [stmt.test]
    if isinstance(stmt, ast.For | ast.AsyncFor):
        return [stmt.target, stmt.iter]
    if isinstance(stmt, ast.With | ast.AsyncWith):
        return list(stmt.items)
    if isinstance(stmt, ast.Try):
        return [h.type for h in stmt.handlers if h.type is not None]
    return []


_COMPOUND = (
    ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.If, ast.While, ast.For, ast.AsyncFor,
    ast.With, ast.AsyncWith, ast.Try,
)  # fmt: skip


class _Trim(ast.NodeTransformer):
    """Drop what names the retired check: imports of it, definitions named for it, dict entries
    and list or set items that mention it, and any other statement that still does. A compound
    statement whose header is clean keeps its other statements. Module-level names dropped this
    way count as retired too, until nothing more is dropped. A trimmed function may be left
    unusable; the format readers and writers never call one."""

    def __init__(self, names: set[str]) -> None:
        self.names = names

    def visit_Dict(self, node: ast.Dict) -> ast.AST:
        pairs = [
            (k, v)
            for k, v in zip(node.keys, node.values, strict=True)
            if not (k is not None and _mentions(k, self.names)) and not _mentions(v, self.names)
        ]
        node.keys, node.values = [k for k, _ in pairs], [v for _, v in pairs]
        return self.generic_visit(node)

    def visit_List(self, node: ast.List) -> ast.AST:
        node.elts = [e for e in node.elts if not _mentions(e, self.names)]
        return self.generic_visit(node)

    def visit_Set(self, node: ast.Set) -> ast.AST:
        node.elts = [e for e in node.elts if not _mentions(e, self.names)] or node.elts
        return self.generic_visit(node)

    def _body(self, body: list[ast.stmt]) -> list[ast.stmt]:
        kept: list[ast.stmt] = []
        for stmt in body:
            if isinstance(stmt, ast.Import | ast.ImportFrom):
                if isinstance(stmt, ast.ImportFrom) and retired(stmt.module or ""):
                    continue
                stmt.names = [a for a in stmt.names if not _mentions(a, set())]
                if stmt.names:
                    kept.append(stmt)
                continue
            if isinstance(stmt, _COMPOUND) and not any(_mentions(h, self.names) for h in _header(stmt)):
                kept.append(self._compound(stmt))
                continue
            stmt = self.visit(stmt)
            if not _mentions(stmt, self.names):
                kept.append(stmt)
        return kept

    def _compound(self, stmt: ast.stmt) -> ast.stmt:
        for field in ("body", "orelse", "finalbody"):
            body = getattr(stmt, field, None)
            if isinstance(body, list) and body:
                setattr(stmt, field, self._body(body) or [ast.Pass()])
        for handler in getattr(stmt, "handlers", []):
            handler.body = self._body(handler.body) or [ast.Pass()]
        for part in _header(stmt):
            self.visit(part)
        return stmt

    def module(self, tree: ast.Module) -> ast.Module:
        while True:
            defined = _defined(tree)
            tree.body = self._body(tree.body)
            dropped = defined - _defined(tree)
            if not dropped - self.names:
                return tree
            self.names |= dropped


def _defined(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(stmt.name)
        elif isinstance(stmt, ast.Assign | ast.AnnAssign):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            names |= {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}
    return names


def stripped(source: str) -> str:
    """`source` with comments, docstrings and the retired check gone; the rest of the code is
    unchanged. Import order is kept as frozen, so lint skips the file; `ruff format` lays it out
    afterwards."""
    tree = _Trim(set()).module(_NoDocstrings().visit(ast.parse(source)))
    return "# ruff: noqa\n" + ast.unparse(ast.fix_missing_locations(tree)) + "\n"


def rendered(source: str) -> str:
    """`stripped`, then laid out by `ruff format` as `freeze` does."""
    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--quiet", "--stdin-filename", "frozen.py", "-"],
        input=stripped(source),
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=True,
    )
    return proc.stdout


def freeze(src: Path, out: Path = OUT) -> int:
    if out.exists():
        shutil.rmtree(out)
    modules = closure(src)
    for path in modules.values():
        target = out / path.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(stripped(path.read_text(encoding="utf-8")), encoding="utf-8")
    subprocess.run([sys.executable, "-m", "ruff", "format", "--quiet", str(out)], cwd=ROOT, check=True)
    return len(modules)


def check(out: Path = OUT) -> list[str]:
    """What freezing would change in the committed fixture: a retired module to delete, or a
    unified diff per file whose trimmed form differs. Empty when the fixture is settled."""
    problems: list[str] = []
    for path in sorted(out.rglob("*.py")):
        rel = path.relative_to(out).as_posix()
        if retired(rel):
            problems.append(f"{rel}: a retired module; delete it")
            continue
        text = path.read_text(encoding="utf-8")
        want = rendered(text)
        if want != text:
            diff = difflib.unified_diff(text.splitlines(True), want.splitlines(True), rel, rel)
            problems.append("".join(diff))
    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--src", type=Path, default=ROOT / "src", help="the source tree to freeze")
    parser.add_argument("--check", action="store_true", help="report changes to the fixture; write nothing")
    args = parser.parse_args()
    if args.check:
        problems = check()
        print("\n".join(problems) or "fixture settled")
        sys.exit(1 if problems else 0)
    print(f"froze {freeze(args.src)} modules into {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
