"""The frozen base stays sanitized; runtime imports and platform branches stay in the fixture.

Audit 10 plan K5 keeps the cases' runtime imports plus the static platform scan, rather than
the full conservative closure produced by `scripts/freeze_format_compat_base.py`.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from format_compat_helpers import run_base

ROOT = Path(__file__).resolve().parents[1]


def _script():
    spec = importlib.util.spec_from_file_location(
        "freeze_base", ROOT / "scripts" / "freeze_format_compat_base.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


freeze = _script()


@pytest.fixture(autouse=True)
def isolated_record(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("FORMAT_COMPAT_IMPORT_RECORD", raising=False)


def unsettled(out: Path, work: Path) -> list[str]:
    """`freeze.check(out)` with one `ruff format` over all files instead of one per file."""
    files = {p.relative_to(out).as_posix(): p for p in sorted(out.rglob("*.py"))}
    problems = [rel for rel in files if freeze.retired(rel)]
    for rel, path in files.items():
        target = work / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(freeze.stripped(path.read_text(encoding="utf-8")), encoding="utf-8")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "format",
            "--quiet",
            "--config",
            str(ROOT / "pyproject.toml"),
            str(work),
        ],
        cwd=ROOT,
        check=True,
    )
    problems += [
        rel
        for rel, path in files.items()
        if (work / rel).read_text(encoding="utf-8") != path.read_text(encoding="utf-8")
    ]
    return problems


def test_the_committed_fixture_is_settled(tmp_path: Path):
    assert unsettled(freeze.OUT, tmp_path) == []


def test_the_fixture_names_neither_the_retired_check_nor_a_feature():
    for path in freeze.OUT.rglob("*"):
        rel = path.relative_to(freeze.OUT).as_posix()
        assert not freeze.retired(rel), rel
        if path.suffix == ".py":
            text = path.read_text(encoding="utf-8")
            assert not freeze.retired(text), rel
            assert not re.search(r"\(feature \d{3}\)", text), rel


def test_the_fixture_is_exactly_the_closure_a_refreeze_takes():
    out = freeze.OUT
    taken = {p.relative_to(out).as_posix() for p in freeze.closure(out).values()}
    assert taken == {p.relative_to(out).as_posix() for p in out.rglob("*.py")}


def test_platform_branches_never_import_a_deleted_frozen_module():
    missing = [
        f"{path.relative_to(freeze.OUT)}:{line}: {name}"
        for path, line, name in freeze.platform_imports(freeze.OUT)
        if freeze._file(freeze.OUT, name) is None
    ]
    assert not missing, "platform imports outside frozen fixture:\n" + "\n".join(missing)


@pytest.mark.parametrize(
    "body",
    [
        'if sys.platform == "win32":\n    IMPORT\n',
        'if os.name == "nt":\n    IMPORT\n',
        'if platform.system() == "Windows":\n    IMPORT\n',
        'FLAG = os.name == "nt"\nif FLAG:\n    IMPORT\n',
        'FLAG = sys.platform\nif FLAG != "win32":\n    pass\nelse:\n    IMPORT\n',
        "if IS_WINDOWS:\n    IMPORT\n",
        "if is_windows():\n    IMPORT\n",
        'if sys.platform != "win32":\n    return\nIMPORT\n',
        'if sys.platform != "win32":\n    raise RuntimeError\nIMPORT\n',
        'if os.name == "nt":\n    def nested():\n        IMPORT\n',
    ],
)
@pytest.mark.parametrize(
    "statement",
    ["import agent_cli.missing", "from ..missing import value", "from .. import missing"],
)
def test_the_platform_scan_sees_lazy_import_forms(tmp_path: Path, body: str, statement: str):
    path = tmp_path / "agent_cli" / "nested" / "reader.py"
    path.parent.mkdir(parents=True)
    text = "def read():\n" + "".join("    " + line for line in body.splitlines(keepends=True))
    text = text.replace("IMPORT", statement)
    path.write_text(text, encoding="utf-8")
    line = next(i for i, row in enumerate(text.splitlines(), 1) if statement in row)
    assert freeze.platform_imports(tmp_path) == [(path, line, "agent_cli.missing")]


def test_the_platform_scan_handles_module_branches_without_keeping_unrelated_imports(tmp_path: Path):
    path = tmp_path / "agent_cli" / "__init__.py"
    path.parent.mkdir()
    path.write_text(
        'if os.name == "nt":\n    from . import missing\n'
        "rows = setup.checks(platform.system())\n"
        "if rows:\n    from . import unused\n",
        encoding="utf-8",
    )
    assert freeze.platform_imports(tmp_path) == [(path, 2, "agent_cli.missing")]


def test_a_refreeze_drops_the_retired_modules_and_trims_what_named_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    word = freeze.RETIRED
    src = tmp_path / "src"
    pkg = src / "agent_cli"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / f"{word}_terms.py").write_text("def find(text):\n    return []\n", encoding="utf-8")
    (pkg / "push_records.py").write_text(
        "from typing import TYPE_CHECKING\n"
        f"from . import {word}_terms\n"
        "from .core import kept\n"
        "if TYPE_CHECKING:\n"
        f"    from .{word}_terms import Update\n"
        f"ROUTES = {{('{word}', 'check'): 'harness', ('doctor', None): 'harness'}}\n"
        f"NAMES = ['{word}_check', 'doctor']\n"
        f"def {word}_check():\n    return 1\n"
        f"def helper(rules: {word}_terms.Rules):\n    return 2\n"
        "def build(root):\n"
        f"    hits = {word}_terms.find(root)\n"
        "    helper(None)\n"
        "    return kept(root)\n",
        encoding="utf-8",
    )
    (pkg / "core.py").write_text("def kept(root):\n    return root\n", encoding="utf-8")
    monkeypatch.setattr(freeze, "ENTRIES", ("agent_cli.push_records",))
    out = tmp_path / "out"
    freeze.freeze(src, out)
    assert sorted(p.name for p in (out / "agent_cli").iterdir()) == [
        "__init__.py",
        "core.py",
        "push_records.py",
    ]
    text = (out / "agent_cli" / "push_records.py").read_text(encoding="utf-8")
    assert not freeze.retired(text)
    assert "ROUTES = {('doctor', None): 'harness'}" in text.replace('"', "'")
    assert "NAMES = ['doctor']" in text.replace('"', "'")
    assert "helper" not in text, "a module-level name dropped for naming it takes its callers along"
    assert "return kept(root)" in text and "from .core import kept" in text
    assert "if TYPE_CHECKING:\n    pass" in text
    assert freeze.check(out) == []


@pytest.mark.parametrize("namespace", [False, True], ids=["package", "namespace"])
def test_the_recorder_keeps_packages_and_lazy_imports(tmp_path: Path, namespace: bool):
    src = tmp_path / "src"
    package = src / "agent_cli" / "nested"
    package.mkdir(parents=True)
    for directory in (package.parent,) if namespace else (package.parent, package):
        (directory / "__init__.py").write_text("", encoding="utf-8")
    (package / "reader.py").write_text("value = 1\n", encoding="utf-8")
    first = run_base(src, tmp_path, "from agent_cli import nested\n")
    second = run_base(src, tmp_path, "def read():\n    from agent_cli.nested import reader\nread()\n")
    assert first.returncode == second.returncode == 0, first.stderr + second.stderr
    record = json.loads((tmp_path / "format-compat-imports.json").read_text(encoding="utf-8"))
    assert record == {
        "loaded": [
            "agent_cli/__init__.py",
            *([] if namespace else ["agent_cli/nested/__init__.py"]),
            "agent_cli/nested/reader.py",
        ],
        "outside": [],
    }


@pytest.mark.parametrize("dynamic", [False, True], ids=["import", "import_module"])
@pytest.mark.parametrize("caught", [False, True], ids=["required", "caught"])
def test_a_missing_frozen_module_fails_and_names_it(tmp_path: Path, caught: bool, dynamic: bool):
    src = tmp_path / "src"
    package = src / "agent_cli"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    code = "import agent_cli.missing\n"
    if dynamic:
        code = "import importlib\nimportlib.import_module('agent_cli.missing')\n"
    if caught:
        code = "try:\n" + "".join(f"    {line}\n" for line in code.splitlines())
        code += "except ModuleNotFoundError:\n    pass\n"
    with pytest.raises(AssertionError, match=r"outside frozen format-compat fixture: agent_cli\.missing"):
        run_base(src, tmp_path, code)


def test_the_recorder_allows_missing_module_probes(tmp_path: Path):
    src = tmp_path / "src"
    package = src / "agent_cli"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    done = run_base(
        src, tmp_path, "import importlib.util\nassert importlib.util.find_spec('agent_cli.new') is None\n"
    )
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("relative", [False, True], ids=["absolute", "relative"])
def test_the_recorder_can_write_a_native_run_to_a_chosen_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: bool
):
    src = tmp_path / "src"
    package = src / "agent_cli"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "reader.py").write_text("value = 1\n", encoding="utf-8")
    record = tmp_path / "native-imports.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FORMAT_COMPAT_IMPORT_RECORD", record.name if relative else str(record))
    first = run_base(src, tmp_path, "from agent_cli import reader\n")
    second = run_base(src, tmp_path, "pass\n")
    assert first.returncode == second.returncode == 0, first.stderr + second.stderr
    assert json.loads(record.read_text(encoding="utf-8")) == {
        "loaded": ["agent_cli/__init__.py", "agent_cli/reader.py"],
        "outside": [],
    }
    assert not (tmp_path / "format-compat-imports.json").exists()


def test_the_recorder_rejects_imports_from_outside_the_fixture(tmp_path: Path):
    src = tmp_path / "src"
    package = src / "agent_cli"
    package.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    (external / "head.py").write_text("value = 1\n", encoding="utf-8")
    (package / "__init__.py").write_text(f"__path__.append({str(external)!r})\n", encoding="utf-8")
    with pytest.raises(AssertionError, match=r"outside frozen format-compat fixture: agent_cli\.head"):
        run_base(src, tmp_path, "import agent_cli.head\n")
