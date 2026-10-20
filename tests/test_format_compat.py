"""State formats read both ways across a slice (`format_compat_helpers`): the
slice base's readers read the head's files (`CASES`), and the head's readers read the base's
(`OLD_CASES`).

`CASES` is the list to extend when a slice changes a state format: `live.json` (p1a's needs-you
and obligation fields) and the dispatch records (consumption and verdict-read fields) are the
ones slice 1 changes. The two `+ new fields` cases add unknown keys to what the head wrote, so
the old readers' tolerance of additive fields is pinned before those changes land."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_cli import dispatch
from agent_cli.dispatch import verdicts
from agent_cli.vault import live
from dispatch_helpers import (
    _new_fast as new_fast,  # pyright: ignore[reportPrivateUsage] -- shared test helper
)
from dispatch_helpers import (
    droot,  # noqa: F401  # pyright: ignore[reportUnusedImport] -- a fixture, used as a parameter name
)
from format_compat_helpers import FormatCase, OldFormatCase, base_source, run_reader, run_writer


def _dispatch(root: Path) -> dict[str, Any]:
    return new_fast(root, watch=False, base="HEAD")


def _live(root: Path) -> None:
    _dispatch(root)
    live.write(root)


def _live_plus(root: Path) -> None:
    _live(root)
    path = live.path(root)
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    assert snapshot["workers"], "the new fields need a worker row to land on"
    snapshot["obligations"] = [{"kind": "verdict", "id": "x", "observed_at": snapshot["rendered_at"]}]
    for row in [*snapshot["workers"], *snapshot["managers"], *snapshot["needs_you"]]:
        row["valid_until"] = snapshot["rendered_at"]
    path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")


def _dispatch_plus(root: Path) -> None:
    d = _dispatch(root)
    path = dispatch.file(root, d["id"], ".json")
    record = json.loads(path.read_text(encoding="utf-8"))
    record.update(consumed_at="2026-10-13T00:00:00+00:00", verdict_read={"generation": 1, "by": "s"})
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


CASES = [
    FormatCase("live.json", _live, ("live.write", "live.build")),
    FormatCase("live.json + new fields", _live_plus, ("live.write", "live.build")),
    FormatCase("dispatch record", _dispatch, ("dispatch.load_all", "dispatch.list", "live.build")),
    FormatCase("dispatch record + new fields", _dispatch_plus, ("dispatch.load_all", "dispatch.list")),
]


@pytest.fixture(scope="session")
def base_src(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return base_source(tmp_path_factory.mktemp("format-compat-base"))


@pytest.fixture
def quiet_supervisor(monkeypatch: pytest.MonkeyPatch) -> None:
    """`dispatch new` records the dispatch; no supervisor process is needed to write the format."""
    monkeypatch.setattr(
        dispatch.supervisor, "popen", type("P", (), {"pid": 1, "__init__": lambda self, *a, **k: None})
    )


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_the_base_reader_reads_the_heads_format(
    droot: Path,  # noqa: F811
    base_src: Path,
    quiet_supervisor: None,
    case: FormatCase,
):
    case.write(droot)
    for reader in case.readers:
        done = run_reader(base_src, droot, reader)
        assert done.returncode == 0, f"base {reader} failed on the head's {case.name}:\n{done.stderr[-2000:]}"


# ---------------------------------------------------------------- old files, head readers


def _head_dispatch(root: Path) -> None:
    """What the head's MCP server, CLI and hooks do with each record: list, load, classify, and
    the verdict-read view a record from before `verdict_read` existed must still answer."""
    records = dispatch.load_all(root)
    assert records, "no dispatch record was read"
    for d in records:
        dispatch.load(root, d["id"])
        dispatch.is_live(d, set())
        verdicts.has_verdict(d)
        verdicts.is_read(d)
    dispatch.list_(root, all_=True)


def _head_live_build(root: Path) -> None:
    live.build(root)


def _head_live_write(root: Path) -> None:
    """The head's supervisor heartbeat over a snapshot an old process wrote (it reads it first)."""
    live.write(root, heartbeat=True)
    json.loads(live.path(root).read_text(encoding="utf-8"))


OLD_CASES = [
    OldFormatCase("dispatch record", ("dispatch.new",), (_head_dispatch, _head_live_build)),
    OldFormatCase("live.json", ("dispatch.new", "live.write"), (_head_live_write,)),
]


def _base_launcher(root: Path) -> None:
    """The base's `launcher.require` on Windows wants the `agent.exe` shim, which `uv sync` still
    writes beside `python.exe`; the fixture root has only `python.exe`."""
    if sys.platform == "win32":
        shim = root / ".venv" / "Scripts" / "agent.exe"
        shim.parent.mkdir(parents=True, exist_ok=True)
        shim.touch()


def _old_format(base_src: Path, root: Path, case: OldFormatCase) -> None:
    _base_launcher(root)
    for writer in case.writers:
        run_writer(base_src, root, writer)
    for reader in case.readers:
        reader(root)


@pytest.mark.parametrize("case", OLD_CASES, ids=[c.name for c in OLD_CASES])
def test_the_head_reader_reads_the_bases_format(
    droot: Path,  # noqa: F811
    base_src: Path,
    case: OldFormatCase,
):
    _old_format(base_src, droot, case)


def test_the_base_writer_wrote_the_old_format(droot: Path, base_src: Path):  # noqa: F811
    """The fixtures are old: a base-written record has no `verdict_read`, which the head's
    `dispatch new` always writes."""
    _base_launcher(droot)
    run_writer(base_src, droot, "dispatch.new")
    [d] = dispatch.load_all(droot)
    assert verdicts.FIELD not in d


def test_a_head_reader_that_rejects_old_records_fails_the_case(droot: Path, base_src: Path):  # noqa: F811
    """The control for the reverse direction (Codex's probe): a head reader that requires
    `verdict_read` fails on a base-written record, so a pass above means the readers coped."""

    def strict(root: Path) -> None:
        for d in dispatch.load_all(root):
            d[verdicts.FIELD]  # noqa: B018 -- the strict reader's whole point

    case = OldFormatCase("strict", ("dispatch.new",), (_head_dispatch, strict))
    with pytest.raises(KeyError, match=verdicts.FIELD):
        _old_format(base_src, droot, case)


def test_a_reader_that_fails_is_reported(droot: Path, base_src: Path):  # noqa: F811
    """The control: an unreadable record makes the base reader exit non-zero, so a pass above
    means the reader ran and coped."""
    record = dispatch.file(droot, "001-01", ".json")
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text('{"feature": 1}\n', encoding="utf-8")
    done = run_reader(base_src, droot, "dispatch.load_all")
    assert done.returncode != 0 and "KeyError" in done.stderr
