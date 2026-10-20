# ruff: noqa
from __future__ import annotations
import shlex
from pathlib import Path
from .worker_redirect import newlines_as_separators, segments

_VALUE_OPTIONS = {
    "-k",
    "-m",
    "-p",
    "-c",
    "-o",
    "-n",
    "-W",
    "-r",
    "--deselect",
    "--ignore",
    "--ignore-glob",
    "--rootdir",
    "--confcache",
    "--maxfail",
    "--basetemp",
    "--durations",
    "--tb",
    "--junitxml",
    "--override-ini",
    "--import-mode",
    "--log-level",
    "--cov",
    "--cov-report",
    "--dist",
    "--numprocesses",
    "--timeout",
    "--capture",
    "--pyargs",
}
REASON = "Workers do not run the full test suite: the supervisor runs the tests your change selects after you return, and the full suite runs once, at landing. Name the test files your change touches, for example `uv run pytest tests/test_x.py`."
_UV_VALUE_OPTIONS = {"--with", "--python", "-p", "--group", "--extra", "--env-file", "--directory"}


def _pytest_args(words: list[str]) -> list[str] | None:
    words = list(words)
    while words and "=" in words[0] and (not words[0].startswith("-")):
        words.pop(0)
    if words[:2] == ["uv", "run"]:
        words = words[2:]
        while words and words[0].startswith("-"):
            option = words.pop(0)
            if option in _UV_VALUE_OPTIONS and words:
                words.pop(0)
    if not words:
        return None
    name = Path(words[0]).name
    if name in ("pytest", "py.test"):
        return words[1:]
    if name.startswith("python") and words[1:3] == ["-m", "pytest"]:
        return words[3:]
    return None


_INFO_ONLY = {"--version", "-V", "--help", "-h"}


def _has_target(args: list[str]) -> bool:
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a in _VALUE_OPTIONS:
            skip = True
            continue
        if a.startswith("-"):
            continue
        if a.rstrip("/") in ("tests", ".", "") or a.startswith(("tests/..", "./tests/..")):
            continue
        if "/" in a or a.endswith(".py") or "::" in a:
            return True
    return False


def full_suite(command: str) -> str | None:
    try:
        lexer = shlex.shlex(newlines_as_separators(command), posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None
    for words in segments(tokens):
        args = _pytest_args(words)
        if args is not None and (not _has_target(args)) and (not set(args) & _INFO_ONLY):
            return REASON
    return None
