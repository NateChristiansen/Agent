"""The CI workflow gates what it runs, and Windows test selection follows tests/windows.txt."""

from __future__ import annotations

import ast
import fnmatch
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
WINDOWS = ROOT / "tests" / "windows.txt"
PENDING = "[pending]"

WINDOWS_NAMES = ("test_win*.py", "*windows*.py")
WINDOWS_CONDITION = re.compile(r"\bos\.name != 'nt'|\bsys\.platform != 'win32'")
WINDOWS_EQUALITY = re.compile(r"\bos\.name == 'nt'|\bsys\.platform == 'win32'")
NATIVE_WINDOWS = re.compile(r"native Windows", re.IGNORECASE)


def _sections() -> tuple[list[str], list[str]]:
    gating: list[str] = []
    pending: list[str] = []
    section = gating
    for line in WINDOWS.read_text(encoding="utf-8").splitlines():
        if line == PENDING:
            section = pending
        elif line and not line.startswith("#"):
            section.append(line)
    return gating, pending


def _is_skip(call: ast.Call) -> bool:
    name = ast.unparse(call.func)
    return name.endswith(("skip", "skipif"))


def _reason(call: ast.Call) -> str:
    texts = [
        node.value
        for node in [*call.args, *(k.value for k in call.keywords)]
        if isinstance(node, ast.Constant)
    ]
    return " ".join(text for text in texts if isinstance(text, str))


def _windows_only(path: Path) -> bool:
    """A skip that runs the test only on Windows: a skipif or an `if ...: pytest.skip` on a
    non-Windows platform, or a skip that names native Windows."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and WINDOWS_CONDITION.search(ast.unparse(node.test)):
            body = ast.Module(body=node.body, type_ignores=[])
            if any(isinstance(n, ast.Call) and _is_skip(n) for n in ast.walk(body)):
                return True
        if isinstance(node, ast.Call) and _is_skip(node):
            condition = (
                ast.unparse(node.args[0]) if ast.unparse(node.func).endswith("skipif") and node.args else ""
            )
            if WINDOWS_CONDITION.search(condition):
                return True
            if NATIVE_WINDOWS.search(_reason(node)) and not WINDOWS_EQUALITY.search(condition):
                return True
    return False


def _windows_step() -> str:
    steps = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["windows-setup"]["steps"]
    [run] = [step["run"] for step in steps if "tests/windows.txt" in step.get("run", "")]
    return run.strip()


def test_every_listed_windows_test_exists_once():
    gating, pending = _sections()
    assert gating, "tests/windows.txt has no gating section"
    listed = gating + pending
    assert [p for p in listed if not (ROOT / p).is_file()] == []
    assert sorted(p for p in set(listed) if listed.count(p) > 1) == []
    assert WINDOWS.read_text(encoding="utf-8").splitlines().count(PENDING) == 1


def test_every_windows_only_test_file_is_listed():
    gating, pending = _sections()
    listed = set(gating + pending)
    unlisted = [
        path.relative_to(ROOT).as_posix()
        for path in sorted((ROOT / "tests").glob("test_*.py"))
        if path.relative_to(ROOT).as_posix() not in listed
        and (any(fnmatch.fnmatch(path.name, name) for name in WINDOWS_NAMES) or _windows_only(path))
    ]
    assert unlisted == [], "add these to tests/windows.txt, gating or [pending]"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('@pytest.mark.skipif(sys.platform != "win32", reason="x")\ndef test_a(): pass\n', True),
        ('m = pytest.mark.skipif(os.name != "nt", reason="x")\n', True),
        ('def test_a():\n    if sys.platform != "win32":\n        pytest.skip("x")\n', True),
        ('def test_a():\n    pytest.skip("a native Windows sharing violation")\n', True),
        (
            '@pytest.mark.skipif(os.name == "nt", reason="native Windows lacks it")\ndef test_a(): pass\n',
            False,
        ),
        ('def test_a():\n    if os.name != "nt":\n        assert mode == 0o600\n', False),
        ('venv.EnvBuilder(symlinks=os.name != "nt")\n', False),
    ],
)
def test_windows_only_detection(tmp_path: Path, source: str, expected: bool):
    path = tmp_path / "test_probe.py"
    path.write_text(source, encoding="utf-8")
    assert _windows_only(path) is expected


def test_windows_setup_runs_exactly_the_gating_section():
    run = _windows_step()
    match = re.fullmatch(r"uv run pytest -n 0 \$\((.+)\)", run)
    assert match, run
    if shutil.which("bash") is None:
        pytest.skip("needs bash to expand the step's file list")
    result = subprocess.run(
        ["bash", "-c", match.group(1)], cwd=ROOT, capture_output=True, text=True, check=True, timeout=30
    )
    assert result.stdout.splitlines() == _sections()[0]


def test_no_job_is_report_only():
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    assert {name for name, job in jobs.items() if job.get("continue-on-error") or job.get("if")} == set()


def test_every_job_has_a_timeout():
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    assert {name: job.get("timeout-minutes") for name, job in jobs.items()} == {
        "pytest": 20,
        "windows-setup": 10,
        "macos-setup": 45,
    }
