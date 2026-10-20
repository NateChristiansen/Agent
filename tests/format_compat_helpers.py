"""Old readers on new state files.

A live process keeps running the code it started with: an old supervisor's heartbeat rewrites
`.agent/live.json`, an old MCP server lists and loads dispatch records. When a slice changes one
of those formats, the slice-base reader must still read the new file without error, or the slice
drains those processes first. This runs the base's reader code, from the slice base's own source,
against state files the head's code wrote.

The other direction too: a file an old process wrote, or one
left from before the landing, must read under the head's code. `BASE_WRITERS` write state with
the base's code from the same extracted source; the head readers run in-process
(`tests/test_format_compat.py`, `OLD_CASES`).

The base source is `tests/fixtures/format_compat_base/`: the base's modules, frozen with
comments and docstrings stripped, so the cases need no git history. It is copied once per test
process under pytest's temporary directory, so an interrupted run leaves nothing behind.
Every base subprocess records its imports in `format-compat-imports.json` beside that copy,
or the file named by `FORMAT_COMPAT_IMPORT_RECORD`. The union across the cases, augmented by
the static platform scan, supplies the kept files (including package initializers); missing
modules fail even if the frozen code catches the import error. Trim the existing fixture,
never refresh historical code from the head to satisfy a new import.

Adding a case (the coordinator, when a builder changes a format):
1. write the new-format file with the head's own writer, in a `FormatCase.write` function;
2. name the base readers that must cope, from `READERS`, or add one there: a few lines of Python
   run with `root` (the harness root, a `Path`) and `agent_cli` imported from the base.
`tests/test_format_compat.py` holds `CASES`; append to it. For the reverse direction, append to
`OLD_CASES`: a `BASE_WRITERS` key (or a new snippet there) and the head readers that must cope.
"""

from __future__ import annotations

import atexit
import builtins
import importlib.abc
import importlib.machinery
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

REPO = Path(__file__).resolve().parents[1]
#: The slice base's readers and writers, frozen by `scripts/freeze_format_compat_base.py`. Move
#: it with `tests/mcp_contract.json` and `tests/hook_names.json`.
BASE = REPO / "tests" / "fixtures" / "format_compat_base"

#: Base readers, by name. Each runs in a fresh interpreter on the base source with `root` bound;
#: it passes when it exits 0. Keep each to what a live old process does with the file.
READERS: dict[str, str] = {
    # an old supervisor or hook rewriting the snapshot over a new-format one (it reads it first)
    "live.write": (
        "import json\n"
        "from agent_cli.vault import live\n"
        "live.write(root, heartbeat=True)\n"
        "json.loads(live.path(root).read_text(encoding='utf-8'))\n"
    ),
    "live.build": "from agent_cli.vault import live\nlive.build(root)\n",
    # an old MCP server or CLI listing, loading and classifying dispatch records
    "dispatch.load_all": (
        "from agent_cli import dispatch\n"
        "records = dispatch.load_all(root)\n"
        "assert records, 'no dispatch record was read'\n"
        "for d in records:\n"
        "    dispatch.load(root, d['id'])\n"
        "    dispatch.is_live(d, set())\n"
    ),
    "dispatch.list": "from agent_cli import dispatch\ndispatch.list_(root, all_=True)\n",
}

#: Base writers, by name: run like `READERS`, they leave state as the base's code writes it.
BASE_WRITERS: dict[str, str] = {
    # `dispatch new` records a queued dispatch; the supervisor process it would start is stubbed.
    # The record is then stripped of the fields newer than the old format (`verdict_read`), so the
    # case holds an old-shaped record whichever source plays the base.
    "dispatch.new": (
        "from agent_cli import dispatch\n"
        "from agent_cli.dispatch import verdicts\n"
        "dispatch.supervisor.popen = type('P', (), {'pid': 1, '__init__': lambda self, *a, **k: None})\n"
        "dispatch.new(root, repo='alpha', feature='1', prompt='format probe', watch=False,\n"
        "             no_checks=True, base='HEAD')\n"
        "for record in dispatch.load_all(root):\n"
        "    if verdicts.FIELD in record:\n"
        "        del record[verdicts.FIELD]\n"
        "        dispatch.save(root, record)\n"
    ),
    "live.write": "from agent_cli.vault import live\nlive.write(root)\n",
}


@dataclass(frozen=True)
class FormatCase:
    """One new-format state: `write` makes it under `root` with the head's code; each of
    `readers` (keys of `READERS`) must then read it under the base's code without error."""

    name: str
    write: Callable[[Path], object]
    readers: tuple[str, ...]


@dataclass(frozen=True)
class OldFormatCase:
    """One old-format state: `writers` (keys of `BASE_WRITERS`) make it under `root` with the
    base's code; each of `readers` (head functions taking `root`) must then read it."""

    name: str
    writers: tuple[str, ...]
    readers: tuple[Callable[[Path], object], ...]


def base_source(into: Path) -> Path:
    """The frozen base's source tree, copied under `into` so its imports write no bytecode into
    the checkout."""
    src = into / "src"
    if not (src / "agent_cli").is_dir():
        shutil.copytree(BASE, src, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return src


def run_reader(base_src: Path, root: Path, reader: str) -> subprocess.CompletedProcess[str]:
    """`READERS[reader]` under the base source, against `root`."""
    return run_base(base_src, root, READERS[reader])


def run_writer(base_src: Path, root: Path, writer: str) -> None:
    """`BASE_WRITERS[writer]` under the base source, against `root`; raises when it fails."""
    done = run_base(base_src, root, BASE_WRITERS[writer])
    assert done.returncode == 0, f"base writer {writer} failed:\n{done.stderr[-2000:]}"


def _record_imports(base_src: Path, record: Path) -> None:
    """Run inside the isolated base interpreter, before its first `agent_cli` import."""
    loaded: set[str] = set()
    outside: set[str] = set()

    def checked(importer: Callable[..., Any]) -> Callable[..., Any]:
        def importing(*args: Any, **kwargs: Any) -> Any:
            try:
                return importer(*args, **kwargs)
            except ModuleNotFoundError as exc:
                name = exc.name or ""
                if name == "agent_cli" or name.startswith("agent_cli."):
                    outside.add(name)
                raise

        return importing

    class Recorder(importlib.abc.MetaPathFinder):
        def find_spec(
            self, fullname: str, path: Sequence[str] | None = None, target: ModuleType | None = None
        ) -> importlib.machinery.ModuleSpec | None:
            if fullname != "agent_cli" and not fullname.startswith("agent_cli."):
                return None
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec is None:
                return None  # `find_spec` probes absent attributes in the lazy dispatch facade.
            if (
                spec is not None
                and spec.origin is None
                and spec.submodule_search_locations
                and all(Path(p).resolve().is_relative_to(base_src) for p in spec.submodule_search_locations)
            ):
                return spec  # Namespace packages have no initializer to keep.
            origin = Path(spec.origin).resolve() if spec is not None and spec.origin else None
            if origin is None or not origin.is_relative_to(base_src):
                outside.add(fullname)
                raise ModuleNotFoundError(f"outside frozen format-compat fixture: {fullname}", name=fullname)
            return spec

    def save() -> None:
        for name, module in sys.modules.copy().items():
            if name == "agent_cli" or name.startswith("agent_cli."):
                origin = getattr(module, "__file__", None)
                if origin and Path(origin).resolve().is_relative_to(base_src):
                    loaded.add(Path(origin).resolve().relative_to(base_src).as_posix())
        previous = json.loads(record.read_text(encoding="utf-8")) if record.exists() else {}
        record.write_text(
            json.dumps(
                {"loaded": sorted(loaded | set(previous.get("loaded", []))), "outside": sorted(outside)}
            )
            + "\n",
            encoding="utf-8",
        )

    sys.meta_path.insert(0, Recorder())
    builtins.__import__ = checked(builtins.__import__)
    importlib.import_module = checked(importlib.import_module)
    atexit.register(save)


def run_base(base_src: Path, root: Path, code: str) -> subprocess.CompletedProcess[str]:
    """`code` under the base source, with `root` bound. `-I` keeps the caller's cwd and
    PYTHONPATH out; the base `src` goes first on the path, ahead of the installed head."""
    record = Path(
        os.environ.get("FORMAT_COMPAT_IMPORT_RECORD", base_src.parent / "format-compat-imports.json")
    ).resolve()
    prelude = (
        "import sys\n"
        "import runpy\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(base_src)!r})\n"
        f"runpy.run_path({__file__!r})['_record_imports'](Path({str(base_src)!r}), Path({str(record)!r}))\n"
        "import agent_cli\n"
        f"assert Path(agent_cli.__file__).is_relative_to({str(base_src)!r}), agent_cli.__file__\n"
        "root = Path(sys.argv[1])\n"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", prelude + code, str(root)],
        cwd=root,
        env={**os.environ, "AGENT_ROOT": str(root)},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert record.exists(), f"base import recorder failed:\n{done.stderr[-2000:]}"
    outside = json.loads(record.read_text(encoding="utf-8"))["outside"]
    assert not outside, f"imports outside frozen format-compat fixture: {', '.join(outside)}"
    return done
