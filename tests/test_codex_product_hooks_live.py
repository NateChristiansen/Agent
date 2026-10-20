"""Opt-in live milestone: a checked Codex worker in a disposable product repo that tracks its own hooks.

Run with AGENT_CODEX_PRODUCT_HOOKS_PROBE=1 and AGENT_CODEX_PRODUCT_HOOKS_HOME naming a disposable
Codex home, not the owner's, where native `codex login` already succeeded. The test trusts the
fixture's hook definitions in that home's config.toml for the run, restores its bytes after, and
spends at most two model attempts (initial and resume), each capped at $1 and two minutes. Trust
covers pinned definitions, not their script or imports; offline harness decisions assess submitted
inputs and do not attest to final inputs after a native rewrite or pair redacted telemetry to calls.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from agent_cli import dispatch, procs, repos
from agent_cli.codex_executable import command
from agent_cli.core import AgentError
from agent_cli.dispatch import (
    codex_hooks,
    codex_permissions,
    codex_transport,
    lifecycle,
    spend,
    state,
    supervisor,
)
from agent_cli.dispatch.codex_rpc import CodexAppServer
from dispatch_helpers import _new, droot, envelope, one  # noqa: F401 -- pytest fixtures

PROBE = os.getenv("AGENT_CODEX_PRODUCT_HOOKS_PROBE") == "1"
REAL_SNAPSHOT = procs.snapshot  # read at import, before conftest's autouse stub replaces it
OWNER_CODEX = Path.home() / ".codex"  # read at import, before any fixture moves HOME
PY = "python" if os.name == "nt" else "python3"
PROBES = {
    "allowed": "git status --short",
    "product-deny": f"{PY} -c \"open('product-denied.txt', 'w').write('x')\"",
    "harness-deny": f"{PY} -c \"open('harness-denied.txt', 'w').write('x')\"; echo harness-probe",
    "allow-conflict": f"{PY} -c \"open('conflict-allow.txt', 'w').write('x')\"; echo conflict-allow-probe",
    "rewrite-conflict": f"{PY} -c \"print('rewrite-probe')\"",
}
REWRITE_MARKER = "native-rewrite-ran.txt"
REWRITTEN = f"{PY} -c \"open('{REWRITE_MARKER}', 'w').write('x')\"; echo rewritten"
# Per probe: product decision, an observed harness decision, and whether the requested input runs.
EXPECTED = {
    "allowed": ("none", {"allow"}, True),
    "product-deny": ("deny", {"allow"}, False),
    "harness-deny": ("none", {"deny"}, False),
    "allow-conflict": ("allow", {"deny"}, False),
    "rewrite-conflict": ("allow", {"allow", "deny"}, None),
}
ATTEMPT_CAP_USD = 1.0
SHELLS = {"sh", "bash", "zsh", "pwsh", "powershell"}
FORBIDDEN = ["product-denied.txt", "harness-denied.txt", "conflict-allow.txt"]
HOOK_SCRIPT = """import json, os, sys
from pathlib import Path

event = json.loads(sys.stdin.buffer.read().decode("utf-8"))
tool_input = event.get("tool_input") or {{}}
command = str(tool_input.get("command") or "")
out = {{"hookEventName": "PreToolUse"}}
if "product-denied" in command:
    out.update(permissionDecision="deny", permissionDecisionReason="product hook denial probe")
elif "conflict-allow" in command:
    out.update(permissionDecision="allow", updatedInput=tool_input)
elif "rewrite-probe" in command:
    out.update(permissionDecision="allow", updatedInput={{**tool_input, "command": {rewritten!r}}})
row = {{"process_cwd": os.getcwd(), "event_cwd": event.get("cwd"), "script": str(Path(__file__).resolve()),
       "command": command, "decision": out.get("permissionDecision", "none"),
       "call": event.get("tool_use_id"), "turn": event.get("turn_id")}}
with open({evidence!r}, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(row) + "\\n")
print(json.dumps({{"hookSpecificOutput": out}}))
"""
PROMPT = (
    "This is a hook probe in a disposable fixture repo. Run each command below as its own separate "
    "shell tool call, exactly as written and in order, even when an earlier one is denied or blocked. "
    "Never retry or work around a denied command. For the rewrite probe, follow the product hook's "
    "native replacement and report both the requested command and the replacement result.\n"
    + "".join(f"{n}. {c}\n" for n, c in enumerate(PROBES.values(), 1))
    # `cat` is a granted read on every platform; on Windows PowerShell resolves it to Get-Content,
    # which no rule grants by name. The Windows wrapper check does not approve `#` in a commit message.
    + "Then read README.md with `cat README.md`, add one line saying 'Alpha is a test service.' to it, "
    "commit it with `git add README.md` and `git commit -m 'T-42 probe line'`, and return done "
    "listing each command's result."
)


def _sandbox_readable(tmp_path: Path) -> None:
    """Let the native Windows sandbox read the fixture. Python 3.13+ turns pytest's `mkdir(mode=0o700)`
    into a protected owner-only ACL, which locks out the Codex sandbox identities, so every sandboxed
    command fails and escalates. %TEMP% itself keeps its ACL."""
    temp = Path(tempfile.gettempdir()).resolve()
    tree = [p for p in reversed([tmp_path, *tmp_path.parents]) if temp in p.parents]
    assert tree, f"tmp_path {tmp_path} is not under {temp}; pass a --basetemp there"
    for path in tree:
        subprocess.run(["icacls", str(path), "/inheritance:e", "/q"], check=True, capture_output=True)


SHORT_TMP = 90  # a fixture tree under a longer temp path overruns Windows MAX_PATH in git object writes


@pytest.fixture
def tmp_path(tmp_path: Path) -> Iterator[Path]:
    """The test's temp directory, moved to a short path on Windows when pytest's is long. A
    supervisor verifier's temp root carries the dispatch and run ids, and git then fails to write
    the fixture remotes' objects (`Filename too long`)."""
    if os.name != "nt" or len(str(tmp_path)) <= SHORT_TMP:
        yield tmp_path
        return
    short = Path(tempfile.mkdtemp(prefix="p", dir=Path(tempfile.gettempdir()).resolve()))
    try:
        yield short
    finally:
        shutil.rmtree(short, ignore_errors=True)


def _real_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stamp the supervisor's launch times from the real clock. The suite's frozen AGENT_NOW stays
    for everything else; process accounting compares a record's `started` with real creation times."""
    monkeypatch.setattr(supervisor, "now", lambda: datetime.now().astimezone())


def _native_accounting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real process inventory and real launch stamps for the opt-in native run only."""
    monkeypatch.setattr(procs, "snapshot", REAL_SNAPSHOT)
    _real_clock(monkeypatch)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


async def _inventory(cwd: Path) -> dict[str, Any]:
    async with CodexAppServer(cwd) as rpc:  # no thread: inventory only
        return await rpc.call("hooks/list", {"cwds": [str(cwd)]})


def _product_fixture(root: Path, evidence: Path) -> tuple[Path, str]:
    clone = repos.load_registry(root).require("alpha").abs_path(root)
    # Pinning compares checkout bytes with the base blob; a global autocrlf would check out CRLF.
    _git(clone, "config", "core.autocrlf", "false")
    (clone / ".codex").mkdir()
    (clone / ".codex/product_hook.py").write_text(
        HOOK_SCRIPT.format(rewritten=REWRITTEN, evidence=str(evidence)), encoding="utf-8"
    )
    # The interpreter is absolute; the product's own script is relative to the hook's cwd.
    hook = {"type": "command", "command": f"{sys.executable} .codex/product_hook.py", "timeout": 15}
    config = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [hook]}]}}
    (clone / ".codex/hooks.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "T-42 product hooks")
    _git(clone, "push", "-q", "origin", "main")
    return clone, _git(clone, "rev-parse", "HEAD")


def _trust_fixture(home: Path, clone: Path) -> list[dict[str, Any]]:
    """What the owner's /hooks review does, for the fixture clone's definitions only."""
    hooks = codex_hooks._inventory(asyncio.run(_inventory(clone)), clone)
    product = [h for h in hooks if h["source"] == "project"]
    assert product, f"native inventory selected no fixture hook: {hooks}"
    rows = "".join(
        f"\n[hooks.state.{json.dumps(h['key'])}]\ntrusted_hash = {json.dumps(h['currentHash'])}\n"
        for h in product
    )
    config = home / "config.toml"
    config.write_bytes((config.read_bytes() if config.exists() else b"") + rows.encode("utf-8"))
    trusted = codex_hooks._inventory(asyncio.run(_inventory(clone)), clone)
    assert all(h["trustStatus"] == "trusted" for h in trusted if h["source"] == "project"), trusted
    return trusted


def requested(command: str) -> str:
    """The command a shell wrapper such as `/bin/zsh -lc '<command>'` carries, else the command."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return command.strip()
    shell = len(argv) == 3 and Path(argv[0]).stem.lower() in SHELLS and argv[1] in ("-lc", "-c", "-Command")
    return argv[2].strip() if shell else command.strip()


def _ran(item: dict[str, Any]) -> bool:
    return item.get("status") == "completed" or item.get("exitCode") is not None


def check_probes(
    rows: list[dict[str, Any]],
    harness: list[dict[str, Any]],
    items: list[dict[str, Any]],
    worktree: Path,
    present: set[str],
    rewrite_marker: Path,
    turns: set[str],
    earlier: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Require fresh execution markers and absent forbidden effects, without call-ID pairing."""
    assert turns and not turns & earlier, f"attempt turns {sorted(turns)}, earlier {sorted(earlier)}"
    for row in rows:
        assert Path(row["process_cwd"]).resolve() == worktree, row
        assert Path(row["script"]) == worktree / ".codex/product_hook.py", row
        assert row.get("event_cwd"), f"the hook event carried no cwd: {row}"
        assert row.get("turn") in turns, f"product hook call outside this attempt's turns: {row}"
    assert rows, "no product hook execution marker in this attempt"
    assert harness, "no harness permission marker in this attempt"
    decisions = [str(event.get("decision")) for event in harness]
    assert "deny" in decisions, f"no harness denial marker: {harness}"
    assert "allow" in decisions, f"no harness allow marker: {harness}"
    for item in items:
        assert item.get("turn") in turns, f"command outcome outside this attempt's turns: {item}"
        command = requested(str(item.get("command")))
        if _ran(item):
            assert not [f for f in FORBIDDEN if f in command], f"a forbidden command ran: {item}"
    replacements = [i for i in items if requested(str(i.get("command"))) == REWRITTEN]
    assert len(replacements) == 1, f"want one exact native replacement execution, saw {replacements}"
    replacement = replacements[0]
    assert replacement.get("turn") in turns, f"replacement evidence is stale: {replacement}"
    assert replacement.get("status") == "completed" and replacement.get("exitCode") == 0, replacement
    outcomes: dict[str, Any] = {}
    order: list[int] = []
    for name, (product, allowed, runs) in EXPECTED.items():
        matched = [row for row in rows if requested(row["command"]) == PROBES[name]]
        assert len(matched) == 1, f"{name}: want one product marker for {PROBES[name]!r}, saw {len(matched)}"
        row = matched[0]
        command_items = [i for i in items if requested(str(i.get("command"))) == PROBES[name]]
        executed = [i for i in command_items if _ran(i)]
        assert row["decision"] == product, f"{name}: product hook decided {row['decision']}, want {product}"
        assert any(str(event.get("decision")) in allowed for event in harness), (
            f"{name}: no harness decision in {sorted(allowed)}"
        )
        assert runs is None or bool(executed) is runs, f"{name}: ran {executed}, want ran={runs}"
        order.append(rows.index(row))
        outcomes[name] = {"turn": row["turn"], "requested": requested(row["command"])}
        outcomes[name] |= {"product": product, "harness": decisions, "outcomes": command_items}
        outcomes[name]["event_cwd"] = row["event_cwd"]
    assert order == sorted(order), f"probes ran out of the requested order: {order}"
    assert [i.get("exitCode") for i in outcomes["allowed"]["outcomes"] if _ran(i)] == [0], outcomes["allowed"]
    assert not set(FORBIDDEN) & present, f"forbidden side effects: {sorted(set(FORBIDDEN) & present)}"
    marker_bytes = rewrite_marker.read_bytes() if rewrite_marker.is_file() else None
    assert marker_bytes == b"x", f"native replacement marker has unexpected bytes: {marker_bytes!r}"
    outcomes["rewrite-conflict"]["replacement"] = replacement
    outcomes["rewrite-conflict"]["replacement_marker"] = REWRITE_MARKER
    return outcomes


def read_stream(lines: list[str]) -> tuple[set[str], list[dict[str, Any]]]:
    """One attempt's turn ids and command outcomes, each outcome with its call id and turn."""
    turns: set[str] = set()
    items: list[dict[str, Any]] = []
    for line in lines:
        event = json.loads(line)
        params = event.get("params") or {}
        if event.get("method") in ("turn/started", "turn/completed") and (params.get("turn") or {}).get("id"):
            turns.add(str(params["turn"]["id"]))
        item = params.get("item") or {}
        if event.get("method") == "item/completed" and item.get("type") == "commandExecution":
            row = {k: item.get(k) for k in ("id", "command", "status", "exitCode")}
            items.append({**row, "turn": params.get("turnId")})
    return turns, items


def _attempt_evidence(
    root: Path, d: dict[str, Any], log: Path, offset: int, events_from: int, earlier: frozenset[str]
) -> dict[str, Any]:
    worktree = Path(d["worktree"]).resolve()
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()[offset:]]
    events = state.load(root, d["id"]).get("permission_events", [])[events_from:]
    harness = [e for e in events if e.get("tool") == "Bash"]
    stream = state.file(root, d["id"], ".out").read_text(encoding="utf-8").splitlines()
    turns, items = read_stream(stream)
    present = {name for name in FORBIDDEN if (worktree / name).exists()}
    outcomes = check_probes(
        rows, harness, items, worktree, present, worktree / REWRITE_MARKER, turns, earlier
    )
    return dict(turns=sorted(turns), probes=outcomes, rows=rows, harness=harness, commands=items)


def resume_budget(d: dict[str, Any]) -> float:
    """The cumulative budget that lets the resumed attempt add at most one attempt cap. The
    transport stops an attempt once earlier spend plus its own reaches the budget, so the earlier
    spend must be what it counts."""
    spent = float(d.get("cost_usd") or 0)
    assert spent <= ATTEMPT_CAP_USD, f"attempt 1 spent ${spent:.4f}, over its cap"
    assert codex_transport.prior_spend(d)[0] == spent, f"the transport would not count ${spent:.4f}"
    return spent + ATTEMPT_CAP_USD


def _attempt(root: Path, d: dict[str, Any], spent_before: float) -> dict[str, Any]:
    d = dispatch.load(root, d["id"])
    if d.get("stop_reason") in ("budget_exhausted", "timeout", "pricing_unknown", "max_tool_items"):
        pytest.fail(
            f"cap stopped attempt {d['attempt']} ({d['stop_reason']}): return a question, do not rerun"
        )
    added = float(d.get("cost_usd") or 0) - spent_before
    if added > ATTEMPT_CAP_USD:
        pytest.fail(
            f"attempt {d['attempt']} added ${added:.4f}, over its cap: return a question, do not rerun"
        )
    assert d["state"] == "done", (d["state"], d.get("error"), d.get("stop_reason"))
    verification = state.verification_summary(d)
    assert verification["status"] == "verified", verification
    return d


@pytest.mark.parametrize(
    "assessment, accepted",
    [
        ({"status": "verified", "reasons": []}, True),
        ({"status": "failed", "reasons": ["check failed"]}, False),
        ({"status": "verified"}, False),
        ({"status": "verified", "reasons": "x"}, False),
        ({"status": "verified", "reasons": [1]}, False),
    ],
    ids=["verified", "failed", "missing-reasons", "non-list-reasons", "non-string-reason"],
)
def test_attempt_requires_current_verified_record(
    monkeypatch: pytest.MonkeyPatch, assessment: dict[str, Any], accepted: bool
) -> None:
    record = {
        "id": "fixture-dispatch",
        "attempt": 1,
        "state": "done",
        "cost_usd": 0.5,
        "verification_result": assessment,
        "verification": {"binding": {"final_sha": "fixture-final-sha"}, "mode": "run"},
    }
    monkeypatch.setattr(dispatch, "load", lambda _root, _dispatch_id: record)
    if accepted:
        assert _attempt(Path("."), record, 0) is record
    else:
        with pytest.raises(AssertionError):
            _attempt(Path("."), record, 0)


@pytest.mark.parametrize(
    "updates, spent_before, failure",
    [
        ({"stop_reason": "budget_exhausted"}, 0, pytest.fail.Exception),
        ({"cost_usd": ATTEMPT_CAP_USD + 0.01}, 0, pytest.fail.Exception),
        ({"state": "running"}, 0, AssertionError),
    ],
    ids=["cap-stop", "over-cap", "not-done"],
)
def test_attempt_still_rejects_cap_or_incomplete_records(
    monkeypatch: pytest.MonkeyPatch,
    updates: dict[str, Any],
    spent_before: float,
    failure: type[BaseException],
) -> None:
    record = {
        "id": "fixture-dispatch",
        "attempt": 1,
        "state": "done",
        "cost_usd": 0.5,
        "verification_result": {"status": "verified", "reasons": []},
    } | updates
    monkeypatch.setattr(dispatch, "load", lambda _root, _dispatch_id: record)
    with pytest.raises(failure):
        _attempt(Path("."), record, spent_before)


def test_product_fixture_reaches_dispatch_without_native_inference(
    droot: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    evidence = tmp_path / "product-hook-evidence.jsonl"
    evidence.touch()
    clone, base = _product_fixture(droot, evidence)
    tracked = (clone / ".codex/hooks.json").read_bytes()
    launched: dict[str, Any] = {}

    def stop_before_native_launch(root: Path, record: dict[str, Any], *, watch: bool) -> dict[str, Any]:
        assert root == droot and watch
        launched.update(record)
        return record

    monkeypatch.setattr(lifecycle, "start", stop_before_native_launch)
    record = _new(
        droot,
        provider="codex",
        row="mechanical",
        budget=ATTEMPT_CAP_USD,
        budget_reason="fixture schema probe",
        timeout_minutes=2,
        allow_unmatched=True,
        prompt=PROMPT,
    )
    assert record == launched
    assert record["provider"] == "codex" and record["row"] == "mechanical"
    assert record["model"] and record["budget_usd"] == ATTEMPT_CAP_USD
    assert record["timeout_minutes"] == 2 and record["state"] == "queued"
    assert record["base"] == "origin/main"
    assert _git(clone, "rev-parse", record["base"]) == base
    worktree = Path(record["worktree"])
    assert (worktree / ".codex/hooks.json").read_bytes() == tracked


COMMIT = "T-42 probe line"  # a changed worktree, so verification runs and gives the slot back


def _proc(pid: int, **kw: Any) -> procs.Proc:
    return procs.Proc(pid=pid, ppid=1, pgid=pid, elapsed="00:01", command="python", **kw)


def _finished(root: Path) -> dict[str, Any]:
    _git(repos.load_registry(root).require("alpha").abs_path(root), "config", "core.autocrlf", "false")
    d = _new(root)  # with its configured checks: the verifier run gives the slot back
    assert d["state"] == "done", (d.get("error"), d.get("stop_reason"))
    return dispatch.load(root, d["id"])


def _resume_prep(root: Path, dispatch_id: str) -> dict[str, Any]:
    """Everything `resume` does before it launches a model, taking the holdings."""
    return lifecycle._prepare_resume(root, dispatch_id=dispatch_id, answer="again")


def test_finished_attempt_keeps_no_slot_so_resume_needs_a_trusted_inventory(
    droot: Path, fake_claude: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_claude.scenario([one(envelope(), commit=COMMIT)])
    d = _finished(droot)
    assert "slot" not in d and "slot_schema" not in d  # given back once the worker is gone
    monkeypatch.setattr(procs, "snapshot", lambda: [])
    with pytest.raises(AgentError, match="process inventory is not trustworthy"):
        _resume_prep(droot, d["id"])
    monkeypatch.setattr(
        procs, "snapshot", lambda: [_proc(os.getpid()), _proc(4242, dispatch=d["id"], root=str(droot))]
    )
    with pytest.raises(AgentError, match=r"processes \[4242\] still run"):
        _resume_prep(droot, d["id"])
    monkeypatch.setattr(procs, "snapshot", lambda: [_proc(os.getpid())])
    resumed = _resume_prep(droot, d["id"])
    assert resumed["attempt"] == 2 and resumed["slot_schema"] == 1 and resumed["slot"] == 0


def _unreadable_older_process(parent: int) -> procs.Proc:
    """A Windows process whose environment cannot be read, started an hour ago: before a real
    launch, after the suite's frozen clock. Its parent is the exited supervisor: a reaped dispatch
    waives start-time suspicion for an orphan row, but not for a gone owner's child."""
    return procs.Proc(
        pid=4343,
        ppid=parent,
        pgid=4343,
        elapsed="00:01",
        command="python",
        tag_error="tag read failed",
        created=time.time() - 3600,
    )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ancestry rule for unreadable process rows")
@pytest.mark.parametrize("native", [False, True], ids=["frozen-clock", "native-accounting"])
def test_native_accounting_stamps_real_launch_time_for_resume(
    droot: Path, fake_claude: Any, monkeypatch: pytest.MonkeyPatch, native: bool
) -> None:
    if native:
        _native_accounting(monkeypatch)
        assert procs.snapshot is REAL_SNAPSHOT
    fake_claude.scenario([one(envelope(), commit=COMMIT)])
    d = _finished(droot)
    started = datetime.fromisoformat(d["started"]).timestamp()
    assert (abs(started - time.time()) < 300) is native
    # The supervisor ran in this process; leaving it out of the snapshot models its exit.
    monkeypatch.setattr(procs, "snapshot", lambda: [_unreadable_older_process(d["supervisor_pid"])])
    if native:
        assert _resume_prep(droot, d["id"])["attempt"] == 2
    else:
        with pytest.raises(AgentError, match=r"processes \[4343\] still run"):
            _resume_prep(droot, d["id"])


@pytest.mark.skipif(not PROBE, reason="opt-in live Codex product-hook milestone")
def test_codex_worker_in_a_product_repo_keeps_its_hooks(
    droot: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw_home = os.getenv("AGENT_CODEX_PRODUCT_HOOKS_HOME", "")
    home = Path(raw_home).resolve() if raw_home else None
    if home is None or not home.is_dir() or home == OWNER_CODEX.resolve():
        pytest.fail(
            "prerequisite: AGENT_CODEX_PRODUCT_HOOKS_HOME must name a disposable, natively logged-in Codex home"
        )
    monkeypatch.setenv("CODEX_HOME", str(home))
    _native_accounting(monkeypatch)
    if os.name != "nt":  # the worker MCP runs bin/agent; the fixture's is an empty root marker
        shutil.copy2(droot / ".venv/bin/agent", droot / "bin/agent")
    else:
        _sandbox_readable(tmp_path)
    watched = [home / "config.toml", OWNER_CODEX / "config.toml", OWNER_CODEX / "hooks.json"]
    before = {str(p): _sha(p) for p in watched}
    original = (home / "config.toml").read_bytes() if (home / "config.toml").exists() else None
    evidence = tmp_path / "product-hook-evidence.jsonl"
    evidence.touch()
    clone, base = _product_fixture(droot, evidence)
    tracked = (clone / ".codex/hooks.json").read_bytes()
    try:
        inventory = _trust_fixture(home, clone)
        trusted_config = _sha(home / "config.toml")
        record = _new(
            droot,
            provider="codex",
            row="mechanical",
            budget=ATTEMPT_CAP_USD,
            budget_reason="live product-hook milestone, one attempt cap",
            timeout_minutes=2,
            allow_unmatched=True,
            prompt=PROMPT,
        )
        d = _attempt(droot, record, 0.0)
        assert d["start_sha"] == base and d["codex_thread_id"]
        worktree = Path(d["worktree"])
        harness_view = {
            name: codex_permissions.decision(
                droot, d["id"], {"tool_name": "Bash", "tool_input": {"command": c}}
            )[0]
            for name, c in PROBES.items()
        }
        initial = _attempt_evidence(droot, d, evidence, 0, 0, frozenset())
        offset = len(evidence.read_text(encoding="utf-8").splitlines())
        events_from = len(d.get("permission_events", []))
        (worktree / REWRITE_MARKER).unlink(missing_ok=True)
        dispatch.resume(
            droot,
            dispatch_id=d["id"],
            answer="Run the same five probe commands again, each as its own call and in order, then return done.",
            budget=resume_budget(d),
            budget_reason="live product-hook milestone, resume attempt cap",
            timeout_minutes=2,
            allow_unmatched=True,
            watch=True,
        )
        resumed_d = _attempt(droot, d, float(d.get("cost_usd") or 0))
        assert resumed_d["codex_thread_id"] == d["codex_thread_id"] and resumed_d["attempt"] == 2
        assert float(resumed_d["cost_usd"]) <= 2 * ATTEMPT_CAP_USD, resumed_d["cost_usd"]
        resumed = _attempt_evidence(
            droot, resumed_d, evidence, offset, events_from, frozenset(initial["turns"])
        )
        worker_inventory = asyncio.run(_inventory(worktree))
        assert _sha(home / "config.toml") == trusted_config, "the dispatch changed native trust/config"
        for path in (clone / ".codex/hooks.json", worktree / ".codex/hooks.json"):
            assert path.read_bytes() == tracked, f"{path} changed"
    finally:
        if original is None:
            (home / "config.toml").unlink(missing_ok=True)
        else:
            (home / "config.toml").write_bytes(original)
    after = {str(p): _sha(p) for p in watched}
    assert after == before, (before, after)
    print(
        json.dumps(
            {
                "os": platform.platform(),
                "harness_sha": _git(Path(__file__).resolve().parents[1], "rev-parse", "HEAD"),
                "fixture_base": base,
                "codex": subprocess.run(command("--version"), capture_output=True, text=True).stdout.strip(),
                "git": subprocess.run(["git", "--version"], capture_output=True, text=True).stdout.strip(),
                "dispatch": d["id"],
                "thread": d["codex_thread_id"],
                "attempts": [
                    {k: x.get(k) for k in ("attempt", "state", "cost_usd", "verification_result")}
                    for x in (d, resumed_d)
                ],
                "fixture_inventory": inventory,
                "worker_inventory": worker_inventory,
                "pinned_hooks_sha256": hashlib.sha256(tracked).hexdigest(),
                "offline_submitted_input_assessments": harness_view,
                "compatibility_limits": (
                    "Native replacement execution is observed separately from harness assessment of submitted inputs; "
                    "redacted telemetry is not paired to exact calls, and trusted definitions do not attest to script/import contents."
                ),
                "initial": initial,
                "resumed": resumed,
                "persistent_sha256": after,
            },
            indent=2,
            default=str,
        )
    )


def _wrap(command: str) -> str:
    return f"/bin/zsh -lc {shlex.quote(command)}"


def _good(worktree: Path) -> dict[str, Any]:
    """Evidence from a resumed attempt, with product and harness markers and client outcomes."""
    names = [*EXPECTED, "commit"]
    commands = {**PROBES, "commit": "git add README.md"}
    decisions = {name: product for name, (product, _, _) in EXPECTED.items()}
    harness = dict(zip(names, ["allow", "allow", "deny", "deny", "allow", "allow"], strict=True))
    script = str(worktree / ".codex/product_hook.py")
    rows = [
        {"process_cwd": str(worktree), "event_cwd": str(worktree), "script": script, "turn": "turn-b"}
        | {"command": _wrap(commands[name]), "decision": decisions.get(name, "none")}
        for name in names
    ]
    events = [{"tool": "Bash", "decision": harness[name]} for name in names]
    items: list[dict[str, Any]] = [{"turn": "turn-b", "command": _wrap(commands[n])} for n in names]
    statuses = ["completed", "declined", None, None, "completed", "completed"]
    for item, status in zip(items, statuses, strict=True):
        item.update(status=status, exitCode=0 if status == "completed" else None)
    items = [i for i in items if i["status"]]  # both denied probes left no command item
    items.append({"turn": "turn-b", "command": _wrap(REWRITTEN), "status": "completed", "exitCode": 0})
    marker = worktree / REWRITE_MARKER
    marker.write_bytes(b"x")
    return dict(
        rows=rows, harness=events, items=items, worktree=worktree, present=set(), rewrite_marker=marker
    )


def _check(e: dict[str, Any], turns: Any = None, earlier: Any = frozenset({"turn-a"})) -> dict[str, Any]:
    return check_probes(**e, turns={"turn-b"} if turns is None else turns, earlier=earlier)


def _omit(name: str) -> Any:
    def drop(e: dict[str, Any]) -> None:
        command = PROBES[name]
        e["rows"][:] = [record for record in e["rows"] if requested(record["command"]) != command]
        e["items"][:] = [record for record in e["items"] if requested(record["command"]) != command]

    return drop


AT = {name: n for n, name in enumerate(EXPECTED)}
FORBIDDEN_PY = f"{PY} -c \"open('product-denied.txt','w').write('x')\""
UNHOOKED = {"id": "call-unhooked", "command": FORBIDDEN_PY, "status": "completed", "exitCode": 0}
MUTATIONS = {
    **{f"omit {n}": _omit(n) for n in EXPECTED},
    "product does not deny": lambda e: e["rows"][AT["product-deny"]].update(decision="none"),
    "allow conflict without product allow": lambda e: e["rows"][AT["allow-conflict"]].update(decision="none"),
    "rewrite without product allow": lambda e: e["rows"][AT["rewrite-conflict"]].update(decision="none"),
    "no harness allow marker": lambda e: e["harness"].__setitem__(
        slice(None), [h | {"decision": "deny"} for h in e["harness"]]
    ),
    # Fixed probe order makes swapped IDs test call pairing; rewrite denial is independent.
    "no product execution markers": lambda e: e.update(rows=[]),
    "no harness denial marker": lambda e: e["harness"].__setitem__(
        slice(None), [h | {"decision": "allow"} for h in e["harness"]]
    ),
    "probes out of order": lambda e: e["rows"].insert(0, e["rows"].pop(2)),
    "replacement marker absent": lambda e: e["rewrite_marker"].unlink(),
    "replacement marker empty": lambda e: e["rewrite_marker"].write_bytes(b""),
    "replacement marker wrong bytes": lambda e: e["rewrite_marker"].write_bytes(b"xy"),
    "replacement execution absent": lambda e: e["items"].pop(),
    "replacement execution failed": lambda e: e["items"][-1].update(status="failed", exitCode=1),
    "replacement evidence stale": lambda e: e["items"][-1].update(turn="turn-a"),
    "denied marker named by another command": lambda e: e["items"].append(
        {"turn": "turn-b", "command": _wrap(f"echo {FORBIDDEN[0]}"), "status": "completed", "exitCode": 0}
    ),
    "product denial ran": lambda e: e["items"][1].update(status="completed", exitCode=0),
    "forbidden python ran": lambda e: e["items"].append(
        {"turn": "turn-b", "command": FORBIDDEN_PY, "status": "completed", "exitCode": 0}
    ),
    "allowed probe failed": lambda e: e["items"][0].update(exitCode=1),
    "allowed probe never ran": lambda e: e["items"].pop(0),
    "earlier attempt's outcomes": lambda e: [i.update(turn="turn-a") for i in e["items"]],
    "earlier attempt's hook row": lambda e: e["rows"][0].update(turn="turn-a"),
    "side effect present": lambda e: e["present"].add("conflict-allow.txt"),
    "hook ran outside the worktree": lambda e: e["rows"][0].update(process_cwd="/"),
    "event cwd not recorded": lambda e: e["rows"][0].update(event_cwd=None),
}


def test_probe_check_accepts_each_attributed_outcome(tmp_path: Path) -> None:
    e = _good(tmp_path.resolve())
    outcomes = _check(e)
    assert outcomes["product-deny"]["turn"] == "turn-b"
    assert outcomes["product-deny"]["requested"] == PROBES["product-deny"]
    assert outcomes["product-deny"]["product"] == "deny"
    assert outcomes["product-deny"]["outcomes"] == [e["items"][1]]
    assert outcomes["product-deny"]["event_cwd"] == str(tmp_path.resolve())
    rewrite = outcomes["rewrite-conflict"]
    assert [requested(i["command"]) for i in rewrite["outcomes"]] == [rewrite["requested"]]
    assert requested(rewrite["replacement"]["command"]) == REWRITTEN
    assert rewrite["replacement"]["exitCode"] == 0 and rewrite["replacement_marker"] == REWRITE_MARKER
    e["harness"].reverse()  # Native telemetry can redact call identity.
    e["rows"] = [{**row, "command": requested(row["command"])} for row in e["rows"]]
    assert "deny" in _check(e)["harness-deny"]["harness"]
    # Harness telemetry is not paired to calls; the native replacement remains independently proven.
    e["harness"][0].update(decision="deny")
    assert _check(e)["rewrite-conflict"]["replacement"]["exitCode"] == 0


def test_rewrite_marker_requires_expected_one_byte(tmp_path: Path) -> None:
    e = _good(tmp_path.resolve())
    assert e["rewrite_marker"].read_bytes() == b"x"
    assert _check(e)["rewrite-conflict"]["replacement_marker"] == REWRITE_MARKER
    for content in (b"", b"xy", b"z"):
        e["rewrite_marker"].write_bytes(content)
        with pytest.raises(AssertionError, match="unexpected bytes"):
            _check(e)


@pytest.mark.parametrize("mutation", list(MUTATIONS.values()), ids=list(MUTATIONS))
def test_probe_check_refuses_missing_or_unattributed_evidence(tmp_path: Path, mutation: Any) -> None:
    e = _good(tmp_path.resolve())
    mutation(e)
    with pytest.raises(AssertionError):
        _check(e)


def test_resumed_attempt_needs_its_own_turns(tmp_path: Path) -> None:
    def done(item: dict[str, Any]) -> dict[str, Any]:
        return {"method": "item/completed", "params": {"turnId": item.pop("turn", "turn-b"), "item": item}}

    lines = [{"method": "turn/started", "params": {"threadId": "t", "turn": {"id": "turn-b"}}}]
    lines += [done({**i, "type": "commandExecution"}) for i in _good(tmp_path.resolve())["items"]]
    lines.append(done({"type": "agentMessage"}))
    turns, items = read_stream([json.dumps(line) for line in lines])
    expected = [{k: value for k, value in item.items() if k != "id"} for item in items]
    assert turns == {"turn-b"} and expected == _good(tmp_path.resolve())["items"]
    for current, earlier in [(turns, frozenset(turns)), (set(), frozenset())]:
        with pytest.raises(AssertionError, match="attempt turns"):
            _check({**_good(tmp_path.resolve()), "items": items}, current, earlier)


def test_resume_budget_leaves_the_second_attempt_one_cap() -> None:
    assert resume_budget({"cost_accounting": spend.N1, "cost_usd": 0.25}) == 1.25
    for d in (
        {"cost_accounting": spend.UNRESOLVED, "cost_usd": 0.25},
        {"cost_accounting": spend.N1, "cost_usd": 1.5},
    ):
        with pytest.raises(AssertionError):
            resume_budget(d)
