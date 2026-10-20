"""Validate approved timing-flake quarantine and one serial retry of a failed full run. Require
complete evidence from both runs; unrelated failures, missing tests and malformed node ids cannot
earn a pass.

Read `tests/quarantine.toml` from committed local main, an ancestor of the certified tree: a branch
cannot approve its own flake. Require a valid owner approval, reason, evidence and unexpired
timing-flake entry for every failed node. Missing, pending or malformed entries grant nothing.

Only pytest exit 1 is retryable, with clean completed checkout, all other commands passing,
complete summary node counts and no lost worker, collection error or interruption. Serial
collect-only must match the original execution count. Rerun exactly the failed node ids once, with
project addopts, xdist and rerunfailures disabled and PYTEST_ADDOPTS cleared.

Keep immutable failed and retry evidence under `<store>/failed/` and `<store>/retry/`. A
retry-passed full certificate names both; readers rederive it and fail closed on tampering.
Certification permits only approved flakes to pass on their single recorded serial rerun of the
same tree.
"""

from __future__ import annotations

import json
import re
import secrets
import shlex
import subprocess
import tomllib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .core import atomic_write
from .records.narrow import as_dict, as_dict_or_none, as_list_or_none, str_list

QUARANTINE = "tests/quarantine.toml"
DISPOSITION = "timing-flake"
#: Cleared project options and blocked plugins: the retry is serial and runs each node once.
RETRY_FLAGS = ("-o", "addopts=", "-p", "no:xdist", "-p", "no:rerunfailures")
#: Environment names the retry runs without (`verify_run.run(unset=...)`).
CLEARED_ENV = ("PYTEST_ADDOPTS",)
PASSED, FAILED = "retry-passed", "retry-failed"
_APPROVED = re.compile(r"approved \d{4}-\d{2}-\d{2}\b")
_FIELDS = ("node", "disposition", "owner", "reason", "expires", "evidence", "owner_approval")
_LINE = re.compile(r"^(?:FAILED|ERROR) (?P<node>\S.*?)(?: - .*)?$", re.M)
#: A summary line's whole text after `FAILED `/`ERROR `, for `ambiguous`.
_WHOLE_LINE = re.compile(r"^(?:FAILED|ERROR) (\S.*)$", re.M)
_TOTALS = re.compile(r"^=*\s*((?:\d+ \w+(?:, )?)+) in [\d.]+s\b.*$", re.M)
_COUNT = re.compile(r"(\d+) (failed|errors?|passed)\b")
#: Outcomes a run reports for a test it executed (an `error` is a setup or teardown error).
_RAN = re.compile(r"(\d+) (passed|failed|skipped|xfailed|xpassed|errors?)\b")
_COLLECTED = re.compile(r"^(\d+) tests? collected\b", re.M)
_GROUP = re.compile(r"@[\w.-]+$")
#: A run that lost a worker, stopped early or failed to collect never qualifies (a crashed xdist worker left
#: an unknown failing test unrun, and its crashed node's serial retry certified the tree).
_INCOMPLETE = re.compile(
    r"node down|crashed while running|worker '?\w+'? crashed|replacing crashed worker"
    r"|maximum crashed workers|^Interrupted\b|errors? during collection|^!{3,}",
    re.M | re.I,
)


@dataclass(frozen=True)
class Entry:
    node: str
    owner: str
    reason: str
    expires: date
    evidence: str
    owner_approval: str

    def approved(self) -> bool:
        return bool(_APPROVED.match(self.owner_approval))

    def live(self, on: date) -> bool:
        """Approved and not expired on `on`: an entry that expires gates normally again."""
        return self.approved() and on <= self.expires


def parse(text: str) -> tuple[list[Entry], list[str]]:
    """The entries of a quarantine file and what is wrong with it. A malformed entry is
    reported and left out, never repaired into a valid one."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return [], [f"{QUARANTINE} is not valid TOML ({exc})"]
    if data.get("version") != 1 or set(data) - {"version", "quarantine"}:
        return [], [f"{QUARANTINE} must hold `version = 1` and `[[quarantine]]` entries only"]
    rows = as_list_or_none(data.get("quarantine", []))
    if rows is None:
        return [], [f"{QUARANTINE}: `quarantine` is not a list of entries"]
    entries: list[Entry] = []
    problems: list[str] = []
    for i, raw in enumerate(rows):
        row = as_dict_or_none(raw)
        if row is None or set(row) != set(_FIELDS):
            problems.append(f"entry {i + 1} must have exactly {', '.join(_FIELDS)}")
            continue
        expires = row["expires"]
        if isinstance(expires, str):
            try:
                expires = date.fromisoformat(expires)
            except ValueError:
                expires = None
        texts = [row[k] for k in _FIELDS if k != "expires"]
        if not isinstance(expires, date) or isinstance(expires, datetime):
            problems.append(f"entry {i + 1}: `expires` is not a date")
        elif not all(isinstance(v, str) and v.strip() for v in texts):
            problems.append(f"entry {i + 1}: every field but `expires` is a nonempty string")
        elif row["disposition"] != DISPOSITION:
            problems.append(f"entry {i + 1}: the only disposition is {DISPOSITION!r}")
        elif "::" not in str(row["node"]) or _GROUP.search(str(row["node"])):
            problems.append(f"entry {i + 1}: `node` is a pytest node id without an xdist group suffix")
        else:
            entries.append(
                Entry(
                    str(row["node"]),
                    str(row["owner"]),
                    str(row["reason"]),
                    expires,
                    str(row["evidence"]),
                    str(row["owner_approval"]),
                )
            )
    return entries, problems


def _git(main: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(["git", *args], cwd=main, capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout


def entries_at(main: Path, commit: str) -> list[Entry]:
    """The entries in `commit`'s quarantine file; none when it is absent or unreadable."""
    code, text = _git(main, "show", f"{commit}:{QUARANTINE}")
    return parse(text)[0] if code == 0 else []


def source_for(main: Path, sha: str) -> str | None:
    """Local main, when it is `sha` or an ancestor of it: the only tree whose quarantine counts."""
    code, out = _git(main, "rev-parse", "--verify", "--quiet", "refs/heads/main^{commit}")
    head = out.strip()
    if code or not head:
        return None
    return head if _git(main, "merge-base", "--is-ancestor", head, sha)[0] == 0 else None


def node_id(reported: str) -> str:
    """A node id as `FAILED`/`ERROR` print it, without the xdist group suffix (`@timing-0`)."""
    return _GROUP.sub("", reported.strip())


def failed_nodes(log: str) -> tuple[list[str], str]:
    """The failed node ids of a pytest summary, or `[]` and why they cannot be trusted: a line
    that names no node (a collection error), or fewer lines than the summary counts."""
    if found := _INCOMPLETE.search(log):
        return [], f"the run did not execute every test ({found.group(0).strip()!r} in its output)"
    totals = _TOTALS.findall(log)
    if not totals:
        return [], "the output has no pytest summary"
    counted = sum(int(n) for n, kind in _COUNT.findall(totals[-1]) if kind != "passed")
    lines = [m.group("node") for m in _LINE.finditer(log)]
    if not lines or len(lines) != counted:
        return [], f"the summary counts {counted} failures and errors but names {len(lines)}"
    if any("::" not in line for line in lines):
        return [], "a failure names no test (a collection or session error)"
    return sorted({node_id(line) for line in lines}), ""


def pytest_index(names: list[str]) -> int | None:
    """The one configured pytest command's index, or None when there is not exactly one."""
    from .dispatch.test_select import is_pytest

    found = [i for i, name in enumerate(names) if is_pytest(name)]
    return found[0] if len(found) == 1 else None


def eligible(evidence: Any, life: dict[str, Any], log: str, names: list[str]) -> tuple[list[str], str]:
    """The failed node ids of a run whose only failure is its pytest command's tests, or `[]` and
    why it may not be retried."""
    index = pytest_index(names)
    if index is None:
        return [], "the verify commands do not hold exactly one pytest command"
    sha = evidence.binding.final_sha
    pytest_rows = [c for c in evidence.commands if c.index == index]
    if [c.index for c in evidence.commands] != list(range(len(names))) or len(pytest_rows) != 1:
        return [], "the run does not cover every configured command"
    for c in evidence.commands:
        if c.index != index and (c.outcome != "exited" or c.exit_code != 0):
            return [], f"command {c.index} `{names[c.index]}` failed too"
    if pytest_rows[0].outcome != "exited" or pytest_rows[0].exit_code != 1:
        return (
            [],
            f"pytest ended {pytest_rows[0].outcome} with exit {pytest_rows[0].exit_code}, not a test failure",
        )
    # The runner stops at the first failing command (`interrupted`, `command_failed`); every
    # command ran, so the failing one was the last.
    stopped = evidence.state == "interrupted" and life.get("reason") == "command_failed"
    if not stopped or not life.get("finished") or life.get("cleanup_confirmed") is not True:
        return [], "the run did not stop on its failing tests with its cleanup confirmed"
    if not (
        evidence.before and evidence.after and evidence.before.clean_at(sha) and evidence.after.clean_at(sha)
    ):
        return [], "the checkout was not clean at the commit before and after the run"
    return failed_nodes(log)


def approved_for(nodes: list[str], entries: list[Entry], on: date) -> tuple[list[str], list[str]]:
    """`(retried, unknown)`: the nodes with a live entry, and those without one."""
    live = {e.node for e in entries if e.live(on)}
    return [n for n in nodes if n in live], [n for n in nodes if n not in live]


def executed(log: str) -> int | None:
    """How many tests the run's last pytest summary says it executed, or None without one."""
    totals = _TOTALS.findall(log)
    if not totals:
        return None
    return sum(int(n) for n, _ in _RAN.findall(totals[-1]))


def collected(log: str) -> int | None:
    """The count a `--collect-only -q` run printed, or None."""
    found = _COLLECTED.findall(log)
    return int(found[0]) if found else None


def launcher(command: str) -> str:
    """The configured pytest command up to and including `pytest` (`uv run pytest`, `python -m
    pytest`): its own targets and options are dropped, so the retry argv is built from scratch
    (`pytest tests` plus the failed ids reran every test)."""
    words = shlex.split(command)
    names = [PurePosixPath(w).name for w in words]
    return " ".join(shlex.quote(w) for w in words[: names.index("pytest") + 1])


def retry_config(config: Any, index: int, nodes: list[str]) -> Any:
    """The retry's two commands. First the configured pytest command, collecting only, serially,
    so the original run's executed count can be checked against the suite it should have run;
    then the launcher alone on exactly `nodes`, serially, once each."""
    from .repos import VerificationCommand, VerificationConfig

    base = config.commands[index]
    collect = f"{base.command} --collect-only -q -o addopts= -p no:xdist"
    retry = f"{launcher(base.command)} " + " ".join(shlex.quote(w) for w in (*RETRY_FLAGS, *nodes))
    return VerificationConfig(
        (
            VerificationCommand(collect, base.timeout_seconds),
            VerificationCommand(retry, base.timeout_seconds),
        ),
        base.timeout_seconds * 2,
    )


def incomplete(original_log: str, retry_log: str) -> str:
    """Why the original run did not execute the whole suite, or "": its summary's executed count
    must equal what the retry's collect-only step found on the same tree. Unknown is a no."""
    ran, suite = executed(original_log), collected(retry_log)
    if ran is None or suite is None:
        return "the original run's completeness cannot be read"
    if ran != suite:
        return f"the original run executed {ran} of {suite} collected tests"
    return ""


def collected_ids(retry_log: str) -> set[str]:
    """The exact node ids the retry's collect-only step listed (`--collect-only -q`)."""
    found = _COLLECTED.search(retry_log)
    section = retry_log[: found.start()] if found else ""
    return {line.strip() for line in section.splitlines() if "::" in line and not line.startswith(" ")}


def ambiguous(original_log: str, retry_log: str, nodes: list[str]) -> str:
    """Why the failed node ids read from the summary may not be the tests that failed, or "".

    A summary line is `FAILED <node id>[@group] - <message>`, and a parameter id may itself hold
    ` - ` or `]`, so an unknown failing parameter could read as an approved one. Each line must
    match exactly one node id the same tree collects, and
    those ids must be exactly the retried ones; anything else refuses the retry."""
    ids = collected_ids(retry_log)
    if not ids:
        return "the suite's node ids cannot be read, so the failed ids cannot be confirmed"
    resolved: set[str] = set()
    for line in _WHOLE_LINE.findall(original_log):
        matches = [n for n in ids if re.fullmatch(re.escape(n) + r"(?:@[\w.-]+)?(?: - .*)?", line, re.S)]
        if len(matches) != 1:
            return f"the failure line {line[:120]!r} names {len(matches)} collected tests, not one"
        resolved.add(matches[0])
    if resolved != set(nodes):
        return "the failed tests read exactly are not the retried ones"
    return ""


def _today(stamp: str) -> date:
    try:
        return datetime.fromisoformat(stamp).astimezone(UTC).date()
    except ValueError:
        return datetime.now(UTC).date()


def failed_record(store: Path, record: dict[str, Any]) -> Path:
    """Write the failed run's record once; it is never changed afterwards."""
    path = store / "failed" / f"{record['run_id']}.json"
    if not path.exists():
        atomic_write(path, json.dumps(record, indent=1))
    return path


def write_retry(store: Path, record: dict[str, Any]) -> Path:
    path = store / "retry" / f"{record['run_id']}.json"
    atomic_write(path, json.dumps(record, indent=1))
    return path


@dataclass(frozen=True)
class Attempt:
    """What became of a failed full run: its failed nodes, the retry block a pass record carries
    (None when nothing was retried), and a sentence for the failure message."""

    failed: list[str]
    retry: dict[str, Any] | None
    note: str

    @property
    def passed(self) -> bool:
        return self.retry is not None and self.retry.get("kind") == PASSED


def attempt(
    root: Path,
    rp: Any,
    sha: str,
    tree: str,
    run_id: str,
    outcome: dict[str, Any],
    why: str,
    names: list[str],
    started: str,
) -> Attempt:
    """Record a failed full run, then retry its approved timing flakes once, serially, when every
    failed node is one. Call it with the run's evidence already read; it holds a lane for the
    retry exactly as the full run did."""
    from . import verify_record, verify_run
    from .dispatch.lane import record_lane, wait_limit

    store, main = verify_record.store(root), verify_record.main_of(root)
    where = store / "runs" / run_id
    evidence = outcome["evidence"]
    try:
        life = as_dict(json.loads((where / "lifecycle.json").read_text(encoding="utf-8")))
        log = (where / "output.log").read_text(encoding="utf-8", errors="replace")
    except OSError, ValueError:
        life, log = dict[str, Any](), ""
    nodes, why_not = eligible(evidence, life, log, names) if evidence is not None else ([], "no evidence")
    shown = nodes or failed_nodes(log)[0]
    failed_path = failed_record(
        store,
        {
            "schema": 1,
            "kind": "failed",
            "sha": sha,
            "tree": tree,
            "run_id": run_id,
            "evidence": outcome["hashes"],
            "why": why,
            "failed_nodes": shown,
            "started": started,
            "completed": verify_record._now(),  # pyright: ignore[reportPrivateUsage]
        },
    )
    if why_not:
        return Attempt(shown, None, f"not retried: {why_not}")
    source = source_for(main, sha)
    if source is None:
        return Attempt(nodes, None, "not retried: local main is not an ancestor of this commit")
    retried, unknown = approved_for(nodes, entries_at(main, source), _today(started))
    if unknown:
        return Attempt(
            nodes, None, f"not retried: no approved, unexpired quarantine entry for {', '.join(unknown)}"
        )
    index = pytest_index(names)
    assert index is not None  # eligible checked it
    config = retry_config(rp.verification, index, retried)
    retry_id = secrets.token_hex(16)
    runs = store / "runs"
    began = verify_record._now()  # pyright: ignore[reportPrivateUsage]
    with record_lane(
        root, rp, retry_id, wait_limit(rp), settled=lambda: verify_run.settle(main, runs / retry_id)
    ):
        again = verify_run.run(main, rp, sha, retry_id, runs, config=config, unset=CLEARED_ENV)
    failure = _retry_failure(runs / retry_id, again, config, log, retried)
    kind = FAILED if failure else PASSED
    block = {
        "kind": kind,
        "run_id": retry_id,
        "failed_run": run_id,
        "nodes": retried,
        "source": source,
        "command": config.commands[1].command,
        "evidence": again["hashes"],
    }
    write_retry(
        store,
        {
            "schema": 1,
            **block,
            "sha": sha,
            "tree": tree,
            "failed_record": failed_path.name,
            "why": failure or "",
            "started": began,
            "completed": verify_record._now(),  # pyright: ignore[reportPrivateUsage]
        },
    )
    note = f"{kind}: {len(retried)} approved timing flake(s) rerun once, serially (run {retry_id})"
    return Attempt(nodes, block, note + (f"; {failure}" if failure else ""))


def _retry_failure(
    where: Path, outcome: dict[str, Any], config: Any, original_log: str, nodes: list[str]
) -> str:
    """Why the retry run does not certify, or "": it passed cleanly, and its collect-only step
    shows the original run executed every collected test and failed exactly `nodes`."""
    from . import verify_record

    if outcome["changed"]:
        return "the retry changed the isolated checkout"
    if outcome["evidence"] is None:
        return "the retry left no evidence"
    try:
        life = json.loads((where / "lifecycle.json").read_text(encoding="utf-8"))
        log = (where / "output.log").read_text(encoding="utf-8", errors="replace")
    except OSError, ValueError:
        return "the retry left no readable evidence"
    names = [c.command for c in config.commands]
    failed = verify_record._failure(outcome["evidence"], life, log, len(names), names)  # pyright: ignore[reportPrivateUsage]
    return failed or incomplete(original_log, log) or ambiguous(original_log, log, nodes)


def check(root: Path, record: dict[str, Any], evidence: Any, life: dict[str, Any], log: str) -> str | None:
    """Why a record's retry does not certify its tree, or None when it does: the original run
    failed only on approved, live quarantine entries of an ancestor main, and the retry of exactly
    those nodes, with the recomputed serial command, passed cleanly on the same commit."""
    from . import verify_record, verify_run
    from .dispatch.test_select import with_extra
    from .dispatch.verification_evidence import Evidence, config_digest

    retry = as_dict(record.get("retry"))
    if retry.get("kind") != PASSED:
        return "its retry did not pass"
    rp = verify_record.harness_repo(root)
    if rp.verification is None:
        return "the harness has no verify commands"
    names = [c.command for c in with_extra(rp.verification, str_list(record.get("extra"))).commands]
    nodes, why = eligible(evidence, life, log, names)
    if why:
        return f"the retried run may not be retried ({why})"
    if nodes != retry.get("nodes"):
        return "the retry names other nodes than the failed run"
    main = verify_record.main_of(root)
    source = str(retry.get("source") or "")
    if not source or _git(main, "merge-base", "--is-ancestor", source, str(record.get("sha")))[0] != 0:
        return "the retry's quarantine source is not an ancestor of the certified commit"
    retried, unknown = approved_for(nodes, entries_at(main, source), _today(str(record.get("started") or "")))
    if unknown or retried != nodes:
        return f"not every failed node had an approved, unexpired quarantine entry ({', '.join(unknown)})"
    run_id = str(retry.get("run_id") or "")
    hashes = as_dict(retry.get("evidence"))
    run_dir = verify_record.store(root) / "runs" / run_id
    if not verify_record._RUN_ID.fullmatch(run_id):  # pyright: ignore[reportPrivateUsage]
        return "the retry names no valid run"
    for name in verify_run.EVIDENCE_FILES:
        if hashes.get(name) is None or verify_record._file_sha(run_dir / name) != hashes.get(name):  # pyright: ignore[reportPrivateUsage]
            return f"the retry evidence ({name}) is missing or changed"
    index = pytest_index(names)
    assert index is not None  # eligible checked it
    config = retry_config(rp.verification, index, nodes)
    try:
        again = Evidence.from_dict(json.loads((run_dir / "terminal.json").read_text(encoding="utf-8")))
        again_life = json.loads((run_dir / "lifecycle.json").read_text(encoding="utf-8"))
        again_log = (run_dir / "output.log").read_text(encoding="utf-8", errors="replace")
    except OSError, ValueError, TypeError:
        return "the retry evidence is unreadable"
    b = again.binding
    if (b.dispatch_id, b.run_id, b.final_sha, b.config_digest) != (
        verify_run.RUN_ID,
        run_id,
        record.get("sha"),
        config_digest(config),
    ):
        return "the retry evidence belongs to another run, commit or command"
    retry_names = [c.command for c in config.commands]
    failed = verify_record._failure(again, again_life, again_log, len(retry_names), retry_names)  # pyright: ignore[reportPrivateUsage]
    if failed:
        return f"the retry did not pass ({failed})"
    return incomplete(log, again_log) or ambiguous(log, again_log, nodes) or None


def doctor_rows(main: Path, today: date | None = None) -> list[tuple[str, str]]:
    """`(level, detail)` rows for doctor: malformed, pending and expired entries in the main
    checkout's quarantine file. An expired entry gates normally again; doctor says so."""
    path = main / QUARANTINE
    if not path.is_file():
        return []
    try:
        entries, problems = parse(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return [("warn", f"{QUARANTINE} is unreadable ({exc.strerror})")]
    today = today or datetime.now(UTC).date()
    rows = [("fail", f"{QUARANTINE}: {p}") for p in problems]
    expired = [e.node for e in entries if e.expires < today]
    pending = [e.node for e in entries if not e.approved() and e.expires >= today]
    if expired:
        rows.append(("warn", f"quarantine expired, these gate normally again: {', '.join(expired)}"))
    if pending:
        rows.append(("warn", f"quarantine awaiting owner approval, not retried: {', '.join(pending)}"))
    live = len([e for e in entries if e.live(today)])
    if not rows:
        rows.append(("ok", f"{live} approved timing-flake entr{'y' if live == 1 else 'ies'}"))
    return rows
