"""The (op name, kwargs, argv, mutates) contract table: every op belongs in it.

Three checks run over it (verify speed, phase 3):

- `test_every_op_runs_clean`: every op runs, writes nothing to stdout (anything an op prints
  corrupts the MCP wire), returns an `Op` with a human line and exit code 0, and its data
  serializes to JSON.
- `test_the_cli_passes_its_argv_to_the_op`: the CLI is a thin wrapper, so for every row the CLI
  is called with `argv` and must hand the op the row's kwargs.
- `test_cli_json_is_the_op_data`: a few read-only rows end to end, the CLI's `--json` output
  byte for byte against the op's data on the same root.

The CLI-vs-op comparison used to run for every row against twin roots (two copytrees and 24 CLI
calls per row); the wiring check plus the end-to-end sample cover the same contract.

The few outside things the table touches (the `claude` stub, the Obsidian CLI, the dispatch
nonce and uuid) are pinned once by the `externals` fixture. `test_ops_behaviour.py` carries the
rest: the CLI text-match tests, feature-close/worktree-cleanup tests and dispatch/journal/ledger
tests.
"""

from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import seeded
from agent_cli import codex_mcp, dispatch, obsidian, ops
from agent_cli.core import json_default
from harness_root import FIXED_NOW, copy_notes, link_notes
from test_dispatch import PROMPT

# (op name, kwargs, argv, mutates). `mutates` marks rows whose op writes, which the end-to-end
# CLI comparison leaves out (it runs the CLI and the op on the same root).
CASES = [
    (
        "feature_new",
        {
            "slug": "z",
            "title": "Z",
            "tickets": ["T-1"],
            "repos": ["alpha"],
            "tags": ["t"],
            "use": True,
            "session_id": "s9",
        },
        [
            "feature",
            "new",
            "z",
            "--title",
            "Z",
            "--ticket",
            "T-1",
            "--repo",
            "alpha",
            "--tag",
            "t",
            "--use",
            "--session-id",
            "s9",
        ],
        True,
    ),
    ("feature_list", {}, ["feature", "list"], False),
    ("feature_list", {"include_closed": True}, ["feature", "list", "--all"], False),
    ("feature_list", {"status": ["active"]}, ["feature", "list", "--status", "active"], False),
    ("feature_show", {"feature": "1"}, ["feature", "show", "1"], False),
    (
        "feature_show",
        {"feature": "current", "session_id": "s1"},
        ["feature", "show", "current", "--session-id", "s1"],
        False,
    ),
    (
        "feature_use",
        {"feature": "2", "session_id": "s2"},
        ["feature", "use", "2", "--session-id", "s2"],
        True,
    ),
    ("feature_current", {"session_id": "s1"}, ["feature", "current", "--session-id", "s1"], False),
    (
        "feature_set",
        {"feature": "1", "title": "X2", "add_ticket": ["T-5"], "add_tag": ["k"], "add_repo": ["alpha"]},
        [
            "feature",
            "set",
            "1",
            "--title",
            "X2",
            "--add-ticket",
            "T-5",
            "--add-tag",
            "k",
            "--add-repo",
            "alpha",
        ],
        True,
    ),
    (
        "feature_close",
        {"feature": "2", "status": "done", "note": "closing"},
        ["feature", "close", "2", "--note", "closing"],
        True,
    ),
    ("raw_preserve", {"feature": "1"}, ["raw", "preserve", "1"], True),
    ("task_list", {"feature": "1"}, ["task", "list", "1"], False),
    ("task_list", {"feature": "1", "open_only": True}, ["task", "list", "1", "--open"], False),
    (
        "task_done",
        {"task": "2", "feature": "1", "note": "abc"},
        ["task", "done", "1", "2", "--note", "abc"],
        True,
    ),
    (
        "task_done",
        {"task": "docs", "feature": "current", "session_id": "s1"},
        ["task", "done", "current", "docs", "--session-id", "s1"],
        True,
    ),
    ("task_undo", {"task": "1", "feature": "1"}, ["task", "undo", "1", "1"], True),
    ("task_add", {"text": "ship it", "feature": "1"}, ["task", "add", "1", "ship it"], True),
    (
        "log",
        {"kind": "surprise", "text": 'it "works" `now`\nline two; $HOME && done', "feature": "1"},
        ["log", "1", "--kind", "surprise", 'it "works" `now`\nline two; $HOME && done'],
        True,
    ),
    (
        "adr_new",
        {"slug": "b", "title": "B", "feature": "1", "status": "accepted"},
        ["adr", "new", "b", "--title", "B", "--feature", "1", "--status", "accepted"],
        True,
    ),
    (
        "adr_new",
        {"slug": "c", "title": "C", "supersedes": 1},
        ["adr", "new", "c", "--title", "C", "--supersedes", "1"],
        True,
    ),
    ("status_render", {}, ["status", "render"], False),
    ("journal_collect", {"date": "2026-09-03"}, ["journal", "collect", "2026-09-03"], False),
    ("journal_collect", {"write": True, "as_json": True}, ["journal", "collect", "--write"], False),
    ("journal_missing", {}, ["journal", "missing"], False),
    ("repos_list", {}, ["repos", "list"], False),
    ("repos_status", {}, ["repos", "status"], False),
    ("repos_status", {"name": "alpha"}, ["repos", "status", "alpha"], False),
    ("session_get", {"session_id": "s1"}, ["session", "get", "--session-id", "s1"], False),
    ("dispatch_ledger", {"by": "feature"}, ["dispatch", "ledger", "--by", "feature"], False),
    # ---------------------------------------------------------------- the nine moved off cli.py
    ("status_check", {}, ["status", "check"], False),
    (
        "session_set",
        {"session_id": "s5", "feature": "1", "cwd": "/tmp/x", "name": "second"},
        ["session", "set", "--session-id", "s5", "--feature", "1", "--cwd", "/tmp/x", "--name", "second"],
        True,
    ),
    ("session_prune", {"days": 14}, ["session", "prune", "--days", "14"], True),
    (
        "capture",
        {"kind": "prompt", "session_id": "s1"},
        ["capture", "--kind", "prompt", "--session-id", "s1"],
        True,
    ),
    ("obsidian_setup", {}, ["obsidian", "setup"], True),
    (
        "obsidian_open",
        {"path": "features/001-x/plan.md"},
        ["obsidian", "open", "features/001-x/plan.md"],
        False,
    ),
    ("obsidian_daily", {}, ["obsidian", "daily"], False),
    ("obsidian_search", {"query": "alpha"}, ["obsidian", "search", "alpha"], False),
    ("doctor", {}, ["doctor"], False),
    ("dispatch_collect", {}, ["dispatch", "collect"], True),
    # ---------------------------------------------------------------- the rest of the surface
    ("repos_sync", {"name": "alpha", "no_clone": True}, ["repos", "sync", "alpha", "--no-clone"], False),
    ("session_list", {}, ["session", "list"], False),
    (
        "lease_take",
        {"area": "vault:src", "session_id": "s1"},
        ["lease", "take", "vault:src", "--session-id", "s1"],
        True,
    ),
    (
        "lease_release",
        {"area": "repo:beta", "session_id": "s1"},
        ["lease", "release", "repo:beta", "--session-id", "s1"],
        True,
    ),
    ("lease_list", {}, ["lease", "list"], False),
    (
        "lease_wait",
        {"area": "repo:alpha", "session_id": "s1", "timeout": 120},
        ["lease", "wait", "repo:alpha", "--session-id", "s1"],
        True,
    ),
    ("scratch_path", {}, ["scratch", "path"], False),
    ("status_line", {}, ["status", "line"], False),
    (
        "journal_write",
        {"date": "2026-09-03", "file": "{BLOCK}"},
        ["journal", "write", "2026-09-03", "--file", "{BLOCK}"],
        True,
    ),
    (
        "dispatch_new",
        {"repo": "alpha", "feature": "1", "prompt": PROMPT, "dry_run": True},
        ["dispatch", "new", "alpha", "--feature", "1", "--prompt", PROMPT, "--dry-run"],
        False,
    ),
]

IDS = [f"{name}:{'-'.join(argv[:2])}:{i}" for i, (name, _, argv, _) in enumerate(CASES)]


@pytest.fixture
def externals(monkeypatch: pytest.MonkeyPatch, fake_claude) -> None:
    """Pin what the CASES table would otherwise read from the machine. `fake_claude` is here for
    the `claude` stub on PATH that `dispatch new --dry-run` needs to resolve `cli_path`. No
    worker ever launches from this file."""
    # no Obsidian CLI: `obsidian open|daily|search` then report the same `skipped` everywhere,
    # whether or not the machine running the suite has the app installed
    monkeypatch.setattr(obsidian, "cli_path", lambda: None)
    # the dry-run preview embeds the two values in it that are random rather than derived: the
    # worker's nonce (`secrets.token_hex`) and its pinned session uuid
    monkeypatch.setattr(dispatch.lifecycle, "make_nonce", lambda dispatch_id: f"{dispatch_id}-beef")
    monkeypatch.setattr(
        dispatch.lifecycle, "uuid", SimpleNamespace(uuid4=lambda: "11111111-2222-3333-4444-555555555555")
    )


@pytest.fixture
def block_file(tmp_path: Path) -> Path:
    """The journal block `journal_write` reads. One file, outside both roots, so the two calls
    write the same text into their own journal/."""
    path = tmp_path / "block.md"
    path.write_text("- shipped the thing\n", encoding="utf-8")
    return path


def _subst(value: Any, block: Path) -> Any:
    """`{BLOCK}` in a case's kwargs or argv -> the block file's absolute path. A case cannot
    name a path the fixtures only make up at run time, so it names this token instead."""
    if isinstance(value, str):
        return value.replace("{BLOCK}", str(block))
    if isinstance(value, dict):
        return {k: _subst(v, block) for k, v in value.items()}
    if isinstance(value, list):
        return [_subst(v, block) for v in value]
    return value


def _seed(run, root: Path) -> None:
    """Identical starting state: two features, tasks, a session mapping, an ADR, a log entry,
    one unassigned capture waiting to be adopted, one held lease, and the worker contract."""
    (root / "harness").mkdir(exist_ok=True)
    (root / "harness" / "worker-contract.md").write_text(
        (Path(__file__).resolve().parents[1] / "harness" / "worker-contract.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    r = ["--root", str(root)]
    run(
        [*r, "capture", "--kind", "prompt", "--stdin"],
        stdin=json.dumps({"session_id": "s1", "hook_event_name": "UserPromptSubmit", "prompt": "hi"}),
    )
    run([*r, "feature", "new", "x", "--title", "X", "--repo", "alpha"])
    run([*r, "feature", "new", "y", "--title", "Y"])
    run([*r, "task", "add", "1", "write the parser"])
    run([*r, "task", "add", "1", "write the tests"])
    run([*r, "task", "add", "1", "write the docs"])
    run([*r, "task", "done", "1", "1"])
    run([*r, "feature", "use", "1", "--session-id", "s1"])
    run([*r, "adr", "new", "a", "--title", "A", "--feature", "1"])
    run([*r, "log", "1", "--kind", "note", "first entry"])
    # one lease, so `lease list` and `status line` have something to report
    run([*r, "lease", "take", "repo:beta", "--session-id", "s1"])
    run([*r, "status", "render"])


@pytest.fixture(scope="session")
def _ops_template(
    tmp_path_factory: pytest.TempPathFactory, _root_template: Path, _notes_template: Path
) -> Path:
    """The seeded root, built once per worker (verify speed, phase 2)."""
    base = tmp_path_factory.mktemp("ops-template")
    root = seeded.base_root(base, _root_template, _notes_template)
    (base / "twin-a").mkdir()
    link_notes(root, copy_notes(_notes_template, base / "twin-a"))
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(obsidian, "cli_path", lambda: None)
        mp.setattr(dispatch.lifecycle, "make_nonce", lambda dispatch_id: f"{dispatch_id}-beef")
        mp.setattr(
            dispatch.lifecycle, "uuid", SimpleNamespace(uuid4=lambda: "11111111-2222-3333-4444-555555555555")
        )
        with seeded.seeding(root) as call:
            _seed(call, root)
    finally:
        mp.undo()
    return base


@pytest.fixture
def ops_root(tmp_root: Path, externals, _ops_template: Path) -> Path:
    seeded.install(_ops_template, tmp_root.parent)
    os.chdir(tmp_root)  # the install replaced the directory tmp_root chdir'd into
    return tmp_root


@pytest.fixture(scope="module")
def _shared_base(tmp_path_factory: pytest.TempPathFactory, _ops_template: Path) -> Path:
    base = tmp_path_factory.mktemp("ops-shared")
    seeded.install(_ops_template, base)
    return base


@pytest.fixture
def shared_root(_shared_base: Path, externals, monkeypatch: pytest.MonkeyPatch) -> Path:
    """One seeded root per module and worker, for rows that write nothing and for the wiring
    check, whose op is replaced. `tmp_root`'s environment without its copy."""
    root = _shared_base / "harness"
    monkeypatch.setenv("AGENT_ROOT", str(root))
    monkeypatch.setenv("AGENT_NOW", FIXED_NOW)
    monkeypatch.setenv("AGENT_NOTIFY", "off")
    monkeypatch.setattr(codex_mcp, "probe", lambda root, entry: (True, "fixture MCP probe"))
    monkeypatch.chdir(root)
    return root


def _root_for(request: pytest.FixtureRequest, mutates: bool) -> Path:
    return request.getfixturevalue("ops_root" if mutates else "shared_root")


def _call_op(root: Path, name: str, kwargs: dict[str, Any], block: Path) -> tuple[Any, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        op = getattr(ops, name)(root, **_subst(kwargs, block))
    return op, out.getvalue()


@pytest.mark.parametrize("name,kwargs,argv,mutates", CASES, ids=IDS)
def test_every_op_runs_clean(request, block_file, name, kwargs, argv, mutates):
    op, stdout = _call_op(_root_for(request, mutates), name, kwargs, block_file)
    assert stdout == "", "an op printed to stdout, which corrupts the MCP wire"
    assert isinstance(op, ops.Op)
    assert op.text  # the human line is there for the CLI and the tool summary
    assert op.code == 0
    json.dumps(op.data, default=json_default)


@pytest.mark.parametrize("name,kwargs,argv,mutates", CASES, ids=IDS)
def test_the_cli_passes_its_argv_to_the_op(
    shared_root, run, monkeypatch, block_file, name, kwargs, argv, mutates
):
    seen: list[dict[str, Any]] = []

    def record(root: Path, **given: Any) -> Any:
        seen.append(given)
        return ops.Op({}, "recorded")

    monkeypatch.setattr(ops, name, record)
    r = run(
        ["--root", str(shared_root), "--json", *_subst(argv, block_file)]
    )  # as the MCP-era table always ran it
    assert r.code == 0, r.err
    assert seen, f"`agent {' '.join(argv)}` never called ops.{name}"
    expected = _subst(kwargs, block_file)
    assert {k: seen[0].get(k) for k in expected} == expected


# Read-only rows whose data the CLI prints, compared end to end on the same root.
END_TO_END = {"feature_list", "feature_show", "task_list", "lease_list"}


@pytest.mark.parametrize(
    "name,kwargs,argv,mutates",
    [c for c in CASES if c[0] in END_TO_END and not c[3]],
    ids=lambda v: str(v)[:20],
)
def test_cli_json_is_the_op_data(shared_root, run, block_file, name, kwargs, argv, mutates):
    r = run(["--root", str(shared_root), "--json", *_subst(argv, block_file)])
    assert r.code == 0, r.err
    op, _ = _call_op(shared_root, name, kwargs, block_file)
    assert json.dumps(op.data, indent=2, default=json_default) + "\n" == r.out
