"""The one `subprocess.Popen.__init__` counter, in the test process and in every Python child.

`tests/suite_cost.py` imports this module in the pytest process. When it measures, it also puts
this directory first on `PYTHONPATH` and names a spawn file in `AGENT_SUITE_STATS_SPAWNS`, so a
Python child that inherits the environment imports it at startup as `sitecustomize` and appends
one byte per process it starts: `.` for a start the counter can follow, `o` for a Python child it
cannot (one started with `-I`, `-E` or `-S`, or with an `env=` that drops this directory or the
spawn file). A byte per start, not a total at exit, so a child that is killed still counts. The
harness's own `-I` verifier children are the exception: they run code from `isolated_code`.

Standard library only. The wrapper installs at import only when `AGENT_SUITE_STATS_SPAWNS` is set.
Imported as `sitecustomize`, it then runs the `sitecustomize` it shadows, the one the child would
have imported without this directory on its path.
"""

from __future__ import annotations

import functools
import os
import subprocess
import sys

TYPE_CHECKING = False  # every Python child imports this file, so `typing` stays unimported
if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

SPAWNS_ENV = "AGENT_SUITE_STATS_SPAWNS"
SITE_DIR = os.path.dirname(os.path.abspath(__file__))
CHILD_MARK = "#suite-cost-child\n"  # in the code of a `-c` child `isolated_code` made followable

count = 0
opaque = 0  # the starts among them whose own spawns this counter cannot see
sink: str | None = None  # the spawn file this process appends to, in a child
watch = False  # classify each start as followable or opaque (only while measuring)
listeners: list[Callable[[Any, dict[str, Any]], None]] = []


def _argv(args: Any) -> list[str]:
    if isinstance(args, (str, bytes, os.PathLike)):
        return os.fsdecode(args).split() if isinstance(args, (str, bytes)) else [os.fsdecode(args)]
    return [os.fsdecode(a) if isinstance(a, (bytes, os.PathLike)) else str(a) for a in args]


def _blind_flags(argv: list[str]) -> bool:
    """`-I`, `-E` or `-S` among the interpreter's own options: no PYTHONPATH or no site."""
    i = 1
    while i < len(argv):
        token = argv[i]
        if not token.startswith("-") or token == "-" or token.startswith("--"):
            return False
        if token[1] in "WX":  # `-X dev` or `-Xdev`
            i += 1 if len(token) > 2 else 2
            continue
        flags = token[1:]
        for ch in "cmWX":  # `-Ic code`: what follows is that option's argument
            flags = flags.split(ch, 1)[0]
        if any(ch in flags for ch in "IES"):
            return True
        if len(flags) < len(token) - 1:
            return False
        i += 1
    return False


def _named_python(exe: str) -> bool:
    return exe == sys.executable or os.path.basename(exe).lower().startswith(("python", "pypy"))


def _python_script(exe: str, env: dict[str, Any]) -> bool:
    import shutil

    path = exe if os.path.dirname(exe) else shutil.which(exe, path=env.get("PATH"))
    try:
        with open(path or exe, "rb") as fh:
            first = fh.readline(200)
    except OSError:
        return False
    return first.startswith(b"#!") and b"python" in first


def isolated_code(code: str, *, starts: bool = True) -> str:
    """`code` for a `-I -c` child, rewritten so the counter follows it; unchanged when not measuring.

    The harness finds this module as `subprocess.suite_cost`, since product code cannot import
    tests and tests replace `subprocess.Popen` itself. `-I` ignores PYTHONPATH and the caller's `env=` may drop the spawn file, so
    the child loads this file by its absolute path and is handed the spawn file in the code itself.
    A child that starts nothing and only execs (`starts=False`) is marked without loading anything:
    what it becomes is followed like any program the parent started directly."""
    target = sink or os.environ.get(SPAWNS_ENV)
    if not watch or not target:
        return code
    if not starts:
        return CHILD_MARK + code
    loader = (
        "import importlib.util as _u, sys as _s; "
        f"_c = _u.spec_from_file_location('suite_cost_counter', {os.path.abspath(__file__)!r}); "
        "_m = _u.module_from_spec(_c); _s.modules[_c.name] = _m; _c.loader.exec_module(_m); "
        f"_m.sink, _m.watch = {target!r}, True; _m.install(); del _u, _s, _c, _m\n"
    )
    return CHILD_MARK + loader + code


def is_opaque(args: Any, kw: dict[str, Any]) -> bool:
    """True when the start is a Python child whose own starts this counter cannot see."""
    argv = _argv(args)
    exe = os.fsdecode(kw.get("executable") or (argv[0] if argv else ""))
    if any(CHILD_MARK in a for a in argv[1:]) and _named_python(exe):
        return False
    env = kw.get("env")
    if env is None or (env.get(SPAWNS_ENV) and SITE_DIR in str(env.get("PYTHONPATH", "")).split(os.pathsep)):
        return _named_python(exe) and _blind_flags(argv)
    return _named_python(exe) or _python_script(exe, env)


def _record(opaque_start: bool) -> None:
    if sink is None:
        return
    try:
        fd = os.open(sink, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    except OSError:
        return
    try:
        os.write(fd, b"o" if opaque_start else b".")
    finally:
        os.close(fd)


def install() -> None:
    """Wrap `Popen.__init__` once; a second call in the same process keeps the first wrapper."""
    current = subprocess.Popen.__init__
    if getattr(current, "suite_cost", None) is not None:
        return

    @functools.wraps(current)
    def counting(self: subprocess.Popen[Any], args: Any, *a: Any, **kw: Any) -> None:
        global count, opaque
        count += 1
        blind = watch and is_opaque(args, kw)
        if blind:
            opaque += 1
        if sink is not None:
            _record(blind)
        for listener in listeners:
            listener(args, kw)
        current(self, args, *a, **kw)

    counting.suite_cost = sys.modules[__name__]  # type: ignore[attr-defined]
    subprocess.Popen.__init__ = counting  # type: ignore[method-assign]
    subprocess.suite_cost = counting.suite_cost  # type: ignore[attr-defined]


def active() -> Any:
    """The module instance whose wrapper is on `Popen.__init__`, installing this one if none is.

    A pytest worker that inherited the spawn file imported this file as `sitecustomize` before the
    plugin loaded it under another name; both are the same counter, and the first one wins."""
    install()
    return subprocess.Popen.__init__.suite_cost  # type: ignore[attr-defined]


def _run_shadowed() -> None:
    """Import the next `sitecustomize` on `sys.path` in this one's place, as Python would have."""
    import importlib.machinery
    import importlib.util

    here = os.path.realpath(SITE_DIR)
    rest = [p for p in sys.path if os.path.realpath(p or os.curdir) != here]
    spec = importlib.machinery.PathFinder.find_spec("sitecustomize", rest)
    if spec is None or spec.loader is None:
        return
    module, ours = importlib.util.module_from_spec(spec), sys.modules[__name__]
    sys.modules["sitecustomize"] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules["sitecustomize"] = ours
        raise


if os.environ.get(SPAWNS_ENV):
    sink = os.environ[SPAWNS_ENV]
    watch = True
    install()

if __name__ == "sitecustomize":
    _run_shadowed()
