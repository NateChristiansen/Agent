"""Shared fixtures: an isolated harness root with a fixed clock and a tiny registry."""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

import obsidian_guard as obsidian_guard_mod
import suite_cost
from agent_cli.cli import main

# Seeded session templates, and the autouse fixture that sets tmp_path up first (seeded.py).
from dispatch_helpers import _droot_template, direct_git  # noqa: F401  (registration)

# The root fixtures live in `harness_root.py`: importing them here is what
# registers `tmp_root` for the whole suite, and the other names are re-exported because test
# modules import FIXED_NOW, REGISTRY and git from conftest.
from harness_root import (  # noqa: F401  (imported for registration and re-export)
    FIXED_NOW,
    REGISTRY,
    _build_root,
    _notes_template,
    _root_template,
    git,
    notes_remote,
    plugin_builds,
    tmp_root,
)

# The live-state fences (isolation.py) are autouse for the whole suite; importing them here is what
# registers them.
from isolation import _live_state_fence, _no_fence_violations  # noqa: F401  (registration)
from land_helpers import _land_template  # noqa: F401  (registration)
from seeded import HOST_IDENTITY, _aa_tmp_path_first  # noqa: F401  (registration)

# The spawn counter's hooks (suite_cost.py); its configure step runs from pytest_configure below.
from suite_cost import (  # noqa: F401  (the hooks)
    pytest_configure_node,
    pytest_runtest_logreport,
    pytest_runtest_logstart,
    pytest_runtest_makereport,
    pytest_sessionfinish,
    pytest_testnodedown,
    pytest_unconfigure,
)
from test_record_capacity import _record_template  # noqa: F401  (registration)
from timing_groups import pytest_collection_modifyitems  # noqa: F401  (the hook)

# The frozen format-compat baseline holds modules named like tests; it is data, never collected.
collect_ignore = ["fixtures/format_compat_base"]


def pytest_configure(config: pytest.Config) -> None:
    suite_cost.configure(config)
    # The candidate's own code, or nothing (origin_guard.py). xdist workers share the
    # controller's environment, so only the controller starts the child-interpreter probe.
    import origin_guard

    origin_guard.check(config.rootpath, with_child=not hasattr(config, "workerinput"))


@pytest.fixture(autouse=True, scope="session")
def _no_real_notifications():
    """Nothing in the suite may reach osascript: dispatch.notify skips macos under
    AGENT_NOTIFY=off, and _macos_notify refuses while AGENT_NOW is set.
    Tests that exercise notify set AGENT_NOTIFY=on and patch the senders to recorders."""
    os.environ["AGENT_NOTIFY"] = "off"
    yield


@pytest.fixture(autouse=True, scope="session")
def _hermetic_git_identity(tmp_path_factory: pytest.TempPathFactory):
    """Git finds an author and committer in a throwaway global config, not the machine's.

    `land` runs `commit-tree` with the committer left to git, so a runner with no global identity
    failed 57 tests. A global file sits below repo config and `-c`, so a test that sets its own
    identity still wins; one that sets `GIT_CONFIG_GLOBAL` itself still gets exactly that. The
    variable is in `os.environ`, so `land` subprocesses inherit it.
    """
    config = tmp_path_factory.mktemp("gitconfig") / "config"
    config.write_text("[user]\n\tname = Test Suite\n\temail = suite@example.invalid\n", encoding="utf-8")
    saved = {k: os.environ.pop(k, None) for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL")}
    saved |= {k: os.environ.pop(k, None) for k in ("GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")}
    previous = os.environ.get("GIT_CONFIG_GLOBAL")
    os.environ["GIT_CONFIG_GLOBAL"] = str(config)
    yield
    if previous is None:
        os.environ.pop("GIT_CONFIG_GLOBAL", None)
    else:
        os.environ["GIT_CONFIG_GLOBAL"] = previous
    for key, value in saved.items():
        if value is not None:
            os.environ[key] = value


@pytest.fixture(autouse=True)
def _no_host_session_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test starts without the caller's Claude or Codex session unless it sets one itself.

    Seven tests failed only when pytest inherited a Codex manager's `CODEX_SESSION_ID`:
    `feature current` found a real session outside each case's isolated fixture root.
    `CLAUDE_PID` belongs here too: `sessions.resolve_provider` reads it as a Claude signal, so
    leaving it set makes every provider test answer for the machine running pytest rather than
    for the case under test. Same leak as one variable over.
    `AGENT_RIBBON_LAUNCH` too: pytest run from a ribbon-launched manager sent every Claude tab
    refresh down the first-claim path and three relabel tests saw no request (2026-09-28).
    The messaging pair too: the activity hook records `CLAUDE_CODE_MESSAGING_SOCKET` as the
    session's inbox, and a test record must not point at the real session's socket (#54).
    """
    for key in HOST_IDENTITY:
        monkeypatch.delenv(key, raising=False)


# A recording `osascript` shim, first on PATH next to the stub `claude`. Nothing should reach
# it (AGENT_NOTIFY=off and the AGENT_NOW guard stop the call earlier). If something does,
# it records an osascript-<n>.json instead of showing a notification on the owner's screen.
FAKE_OSASCRIPT = """#!{python}
import json, os, sys
d = os.environ["FAKE_CLAUDE_DIR"]
n = len([f for f in os.listdir(d) if f.startswith("osascript-")]) + 1
with open(os.path.join(d, "osascript-%d.json" % n), "w") as fh:
    json.dump({{"argv": sys.argv[1:]}}, fh)
"""

# A `claude` that only has to answer `--version`. It is on PATH so `claude_on_path()` resolves
# and `doctor`'s worker-transport check has something to read. It is NEVER executed as a
# worker, because the transport below replaces the SDK's subprocess entirely.
STUB_CLAUDE = """#!{python}
import sys
sys.stdout.write("9.9.9 (Claude Code)\\n")
"""


def _write_stub(directory: Path, name: str, body: str) -> Path:
    script = directory / name
    script.write_text(body.format(python=sys.executable), encoding="utf-8")
    script.chmod(0o755)
    return script


@pytest.fixture(autouse=True)
def _no_real_reaping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing in the suite may signal a real process. A supervisor reaps everything tagged with
    its dispatch id when it ends, and the dispatch tests run the supervisor inline,
    so an unguarded reap would `ps` the owner's machine and could kill a real process that happens
    to carry a fixture's id. The process table is empty here unless a test builds its own; tests
    of the reaper itself hand `procs` their own snapshot and their own planted children.
    Native process tests must restore procs.snapshot before asserting real cleanup."""
    from agent_cli import procs

    monkeypatch.setattr(procs, "snapshot", lambda: [])


# On POSIX `Path.home()` reads $HOME, so a test that sets HOME isolates every home path. On Windows
# `os.path.expanduser` reads USERPROFILE and ignores HOME, so the same tests reached the owner's real
# profile: peer_net enrollment found the real `~/.ssh/agent_peer_windows` and failed `key-exists`,
# and could have written keys there. On Windows the suite expands `~` the POSIX way.
_REAL_EXPANDUSER = os.path.expanduser


def posix_expanduser(path):
    """`~` and `~/...` expand to $HOME when it is set; anything else goes to the platform's own."""
    home = os.environ.get("HOME")
    text = os.fspath(path)
    if home and isinstance(text, str) and (text == "~" or text.startswith(("~/", "~\\"))):
        return home + text[1:]
    return _REAL_EXPANDUSER(path)


@pytest.fixture(autouse=True)
def _windows_home_follows_env(monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform == "win32":
        monkeypatch.setattr(os.path, "expanduser", posix_expanduser)


def real_peer_keys() -> dict[str, int]:
    """The owner's real peer keys and their modification times (USERPROFILE is never redirected
    for a whole session, so it names the real profile)."""
    ssh = Path(os.environ.get("USERPROFILE") or _REAL_EXPANDUSER("~")) / ".ssh"
    return {p.name: p.stat().st_mtime_ns for p in ssh.glob("agent_peer_*")} if ssh.is_dir() else {}


@pytest.fixture(autouse=True, scope="session")
def _real_peer_keys_untouched():
    """Backstop for a subprocess the patch above cannot reach: on Windows a run that changed the
    owner's real peer keys errors at the end, by name."""
    if sys.platform != "win32":
        yield
        return
    before = real_peer_keys()
    yield
    after = real_peer_keys()
    if after != before:
        pytest.fail(f"the suite changed the owner's real ~/.ssh peer keys: {before} -> {after}")


def unregistered_agent_dirs(base: Path) -> list[Path]:
    """Every entry directly under some `.agent/` below `base` (a directory, a file or a link)
    whose name is in neither `core.AGENT_SUBTREES` nor `core.AGENT_UNREGISTERED`.

    Walks the whole tree, registered subtrees included (a nested repo's `.agent/` can sit under
    `.agent/sessions/...`), without following links. A `.agent` that is itself a link is judged
    by its resolved target when that target is inside `base`; a target outside `base` is not
    the test's to judge. Each directory is visited once, by (device, inode), so a link loop or
    a target reached twice cannot repeat or recurse."""
    from agent_cli.core import AGENT_NAMES, agent_dir_name

    folder = agent_dir_name()
    top = base.resolve()
    seen: set[tuple[int, int]] = set()
    judged: set[tuple[int, int]] = set()
    found: list[Path] = []

    def judge(shown: Path, real: Path) -> None:
        """Every entry of one `.agent`, whatever its type, reported under the path that names
        it. A link counts by its own name: `.agent/new-x -> elsewhere` is a new subtree."""
        try:
            info = real.stat()
            if (info.st_dev, info.st_ino) in judged:
                return
            judged.add((info.st_dev, info.st_ino))
            with os.scandir(real) as entries:
                children = [e.name for e in entries]
        except OSError:
            return
        found.extend(shown / name for name in children if name not in AGENT_NAMES)

    stack = [base]
    while stack:
        current = stack.pop()
        try:
            info = current.stat(follow_symlinks=False)
            if (info.st_dev, info.st_ino) in seen:
                continue
            seen.add((info.st_dev, info.st_ino))
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            path = Path(entry.path)
            # A Windows junction is a link too, though `is_dir(follow_symlinks=False)` says dir.
            is_link = entry.is_symlink() or entry.is_junction()
            if entry.name == folder:
                if is_link:
                    target = path.resolve()
                    if target.is_relative_to(top) and target.is_dir():
                        judge(path, target)
                    continue
                if entry.is_dir(follow_symlinks=False):
                    judge(path, path)
            if entry.is_dir(follow_symlinks=False) and not is_link:
                stack.append(path)
    return sorted(found)


def _fail_on_unregistered(base: Path) -> None:
    bad = unregistered_agent_dirs(base)
    if bad:
        pytest.fail(
            "unregistered .agent subtrees were created (add each to core.AGENT_SUBTREE_DIRS/FILES "
            f"or core.AGENT_UNREGISTERED, or build the path with core.agent_path): {[str(p) for p in bad]}",
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def _agent_subtrees_registered(tmp_path: Path, request: pytest.FixtureRequest):
    """The dynamic layer  row 3 (`harness/rules-enforcement.md`): whatever the code a
    test ran built under any `.agent/` in its `tmp_path` must be registered, however the path was
    spelled (`chr()`, concatenation, a computed name), which the static ban in
    `tests/test_prose_rules.py` cannot see. Checked at the test's own teardown, because
    `tmp_path_retention_policy = "failed"` deletes a passing test's `tmp_path` right after; it
    requests `tmp_path`, so it is torn down first. A failure is a teardown error naming the
    directory, in each xdist worker. A test that builds a hostile layout on purpose opts out
    with `@pytest.mark.agent_layout_unchecked("<why>")`; the reason is required."""
    yield
    marker = request.node.get_closest_marker("agent_layout_unchecked")
    if marker is not None:
        assert marker.args and str(marker.args[0]).strip(), "agent_layout_unchecked needs a reason"
        return
    _fail_on_unregistered(tmp_path)


@pytest.fixture(autouse=True, scope="session")
def _agent_subtrees_registered_in_basetemp(tmp_path_factory: pytest.TempPathFactory):
    """The same check over what outlives a test in basetemp: session templates and the
    `tmp_path_factory.mktemp` folders they build."""
    yield
    _fail_on_unregistered(tmp_path_factory.getbasetemp())


@pytest.fixture(autouse=True)
def stub_claude_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test sees a `claude` on PATH, whether or not the machine has one.

    `doctor`'s worker-transport check fails when nothing answers `claude --version`: right on
    a developer's machine, wrong inside the suite, where it passed here and failed on every CI
    run after the 2026-09-15 split. `fake_claude` prepends its own copy and still wins.
    On macOS the same directory links the real git ahead of the `/usr/bin/git` xcrun shim.
    """
    bin_dir = tmp_path / "pathstub"
    bin_dir.mkdir()
    _write_stub(bin_dir, "claude", STUB_CLAUDE)
    if git := direct_git():
        (bin_dir / "git").symlink_to(git)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return bin_dir


@pytest.fixture(autouse=True)
def obsidian_guard(
    request: pytest.FixtureRequest, stub_claude_on_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """No test may reach the owner's real Obsidian (Dock icons during a run, 2026-10-03).
    Recording `obsidian` and `open` stubs go first on PATH and `$OBSIDIAN_CLI` points at the
    stub; an in-process spawn of the real app is refused and fails the test. See obsidian_guard.py.
    `OBSIDIAN_GUARD_LOG=<file>` lists each test that reached a stub, one JSON line per test."""
    log = os.environ.get("OBSIDIAN_GUARD_LOG")
    guard = obsidian_guard_mod.install(stub_claude_on_path, tmp_path / "obsidian-calls", monkeypatch)
    yield guard
    if log and (calls := guard.stub_calls()):
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"test": request.node.nodeid, "calls": calls}) + "\n")
    if guard.blocked:
        pytest.fail(f"a test tried to start the real Obsidian: {guard.blocked}")


# ------------------------------------------------------------------ the fake worker transport
#
# The dispatch tests used to drive a fake `claude` binary and assert on its argv. Under
# ADR-0009 the supervisor is `ClaudeSDKClient`, and `ClaudeSDKClient(options, transport=...)`
# takes a custom `Transport`, so the fake is now a transport object in this process: no
# subprocess, no argv, no stream-json framing of our own. What the supervisor CHOSE is
# asserted against the `ClaudeAgentOptions` it built (the honest equivalent of the old argv
# assertions), and what it SENT is recorded off the transport's `write`.
#
# A scenario is a list of attempts; each attempt is a list of turns. A turn is a dict:
#
#   messages:     raw CLI-shaped dicts to emit, in order (usually ending in a result)
#   await_steer:  hold the LAST message until a further user message arrives, so a steer
#                 folds into this turn and only ONE result comes back. This is the SDK
#                 behaviour that hung the spike's supervisor for 55 minutes
#   sleep:        seconds before emitting anything (drives the timeout path)
#   commit/dirty: real git work in the worktree, as the old fake did
#   end:          close the message stream with no result at all (a crashed worker)
#   hooks:        [{"event": "Stop", "input": {...}}]: fire the in-process hook callbacks the
#                 supervisor registered for that event (ClaudeAgentOptions.hooks, T7 item 4),
#                 the way the CLI does: a `hook_callback` control request carrying the id the
#                 SDK handed out in its `initialize`. Fired BEFORE the turn's messages. The
#                 fake waits for the SDK's response, so the callback has run by the time the
#                 result ends the attempt.
#   permission:   [{"tool": "Bash", "input": {"command": "sleep 30"}, "tool_use_id": ...,
#                 "agent_id": ...}]: ask `can_use_tool` (T8) the way the CLI asks it for a
#                 tool call that matched no allow rule and no deny rule. A `can_use_tool`
#                 control request the worker is blocked inside until the SDK answers. Fired
#                 after `hooks` and before the turn's messages. Each answer is recorded on
#                 `transport.permissions` as {"request", "response"}.
#
# `{{NONCE}}` and `{{SESSION}}` in a message are filled from the work order and the options,
# exactly as the old fake filled them from stdin and argv.


def _fill(value, nonce: str, session: str):
    if isinstance(value, str):
        return value.replace("{{NONCE}}", nonce).replace("{{SESSION}}", session)
    if isinstance(value, dict):
        return {k: _fill(v, nonce, session) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, nonce, session) for v in value]
    return value


class FakeTransport:
    """The minimum of the SDK's Transport contract: answer control requests, play a scripted
    turn per user message, and record everything written."""

    def __init__(self, options, attempt: dict):
        self.options = options
        self.turns = list(attempt.get("turns") or [])
        self.writes: list[dict] = []
        self.prompts: list[str] = []
        self.permissions: list[dict] = []  # one {"request", "response"} per can_use_tool ask
        self.interrupts = 0
        self.session = options.session_id or options.resume or ""
        self.nonce = ""
        self._queue: asyncio.Queue = asyncio.Queue()
        self._ready = False
        self._played = 0

    # -- Transport ---------------------------------------------------------
    async def connect(self) -> None:
        self._ready = True

    def is_ready(self) -> bool:
        return self._ready

    async def end_input(self) -> None:
        pass

    async def close(self) -> None:
        self._ready = False
        await self._queue.put(None)

    async def write(self, data: str) -> None:
        for line in data.splitlines():
            if not line.strip():
                continue
            msg = json.loads(line)
            self.writes.append(msg)
            if msg.get("type") == "control_request":
                await self._control(msg)
            elif msg.get("type") == "user":
                await self._user(msg)

    async def read_messages(self):
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    # -- scripting ---------------------------------------------------------
    async def _control(self, msg: dict) -> None:
        if msg.get("request", {}).get("subtype") == "interrupt":
            self.interrupts += 1
        await self._queue.put(
            {
                "type": "control_response",
                "response": {"subtype": "success", "request_id": msg["request_id"], "response": {}},
            }
        )

    async def _user(self, msg: dict) -> None:
        text = msg["message"]["content"]
        self.prompts.append(text)
        found = re.search(r"^nonce: (\S+)$", text, re.M)
        if found:
            self.nonce = found.group(1)
        if not self.turns:
            # No scenario at all: a worker that starts and says nothing. The old fake `claude`
            # exited 0 with empty stdout here. A transport says it by ending the stream. Without
            # this a test that runs a dispatch without scripting one waits out the 45-minute
            # timeout instead of failing in a second.
            await self._queue.put(None)
            return
        if self._played < len(self.turns):
            turn = self.turns[self._played]
            self._played += 1
            asyncio.get_running_loop().create_task(self._play(turn, len(self.prompts)))

    def hook_callback_ids(self, event: str) -> list[str]:
        """The callback ids the SDK registered for `event`, read off the `initialize` control
        request it wrote at connect. This is the wire form of `ClaudeAgentOptions.hooks`."""
        for msg in self.writes:
            request = msg.get("request") or {}
            if request.get("subtype") == "initialize":
                matchers = (request.get("hooks") or {}).get(event) or []
                return [cid for m in matchers for cid in m.get("hookCallbackIds") or []]
        return []

    def _control_response(self, request_id: str) -> dict | None:
        """The SDK's answer to one control request, off what it wrote back."""
        for msg in self.writes:
            response = msg.get("response") or {}
            if response.get("request_id") == request_id:
                return response
        return None

    async def _await_response(self, request_id: str) -> dict | None:
        for _ in range(500):
            response = self._control_response(request_id)
            if response is not None:
                return response
            await asyncio.sleep(0.01)
        return None

    async def _fire_hooks(self, hooks: list[dict]) -> None:
        for i, hook in enumerate(hooks):
            for callback_id in self.hook_callback_ids(str(hook["event"])):
                request_id = f"fake-hook-{len(self.writes)}-{i}"
                await self._queue.put(
                    {
                        "type": "control_request",
                        "request_id": request_id,
                        "request": {
                            "subtype": "hook_callback",
                            "callback_id": callback_id,
                            "input": hook.get("input") or {},
                        },
                    }
                )
                await self._await_response(request_id)

    async def _ask_permission(self, requests: list[dict]) -> None:
        """One `can_use_tool` control request per entry, blocking on the answer the way the
        worker blocks inside the tool call. This ensures the record is written before the
        turn goes on."""
        for i, req in enumerate(requests):
            request_id = f"fake-perm-{len(self.writes)}-{i}"
            await self._queue.put(
                {
                    "type": "control_request",
                    "request_id": request_id,
                    "request": {
                        "subtype": "can_use_tool",
                        "tool_name": req.get("tool") or "Bash",
                        "input": req.get("input") or {},
                        "tool_use_id": req.get("tool_use_id") or f"toolu_fake_{i}",
                        "agent_id": req.get("agent_id"),
                    },
                }
            )
            self.permissions.append({"request": req, "response": await self._await_response(request_id)})

    async def _play(self, turn: dict, started_at: int) -> None:
        if turn.get("sleep"):
            await asyncio.sleep(float(turn["sleep"]))
        self._side_effects(turn)
        if turn.get("hooks"):
            await self._fire_hooks(list(turn["hooks"]))
        if turn.get("permission"):
            await self._ask_permission(list(turn["permission"]))
        messages = [_fill(m, self.nonce, self.session) for m in (turn.get("messages") or [])]
        hold = messages.pop() if (turn.get("await_steer") and messages) else None
        for message in messages:
            await self._queue.put(message)
        if hold is not None:
            # hold the result until a LATER user message arrives, so the steer lands inside
            # this turn and the supervisor gets one result covering both
            while len(self.prompts) <= started_at:
                await asyncio.sleep(0.01)
            await self._queue.put(hold)
        if turn.get("end"):
            await self._queue.put(None)

    def _side_effects(self, turn: dict) -> None:
        cwd = Path(str(self.options.cwd))
        if turn.get("commit"):
            (cwd / "worker.txt").write_text(turn["commit"] + "\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=cwd, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=worker@example.com",
                    "-c",
                    "user.name=Worker",
                    "commit",
                    "-q",
                    "-m",
                    turn["commit"],
                ],
                cwd=cwd,
                check=True,
            )
        if turn.get("dirty"):
            (cwd / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")


class FakeClaude:
    """The handle a test uses: set the scenario, then read back what the supervisor built and
    sent. `calls()` is one entry per launched attempt: the old fake's `call-<n>.json`."""

    def __init__(self, calls_dir: Path, home: Path):
        self.dir = calls_dir
        self.home = home
        self.attempts: list[dict] = []
        self.transports: list[FakeTransport] = []
        self.notifications: list[tuple] = []  # (channel, ...) recorded by the patched senders

    def scenario(self, attempts: list[dict]) -> None:
        self.attempts = attempts

    def factory(self, options) -> FakeTransport:
        n = len(self.transports)
        attempt = self.attempts[min(n, len(self.attempts) - 1)] if self.attempts else {}
        transport = FakeTransport(options, attempt)
        self.transports.append(transport)
        return transport

    def calls(self) -> list[dict]:
        return [
            {
                "options": t.options,
                "prompt": t.prompts[0] if t.prompts else "",
                "steers": t.prompts[1:],
                "cwd": str(t.options.cwd),
                "env": dict(t.options.env),
                "interrupts": t.interrupts,
            }
            for t in self.transports
        ]

    def osascript_calls(self) -> list[dict]:
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.dir.glob("osascript-*.json"))]


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeClaude:
    """Install the fake transport as `dispatch.transport_factory`, put a `--version`-only
    `claude` and a recording `osascript` first on PATH, isolate HOME, silence notifications,
    and hand back a handle to script the worker and read what the supervisor did."""
    from agent_cli import dispatch as dispatch_mod

    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    calls_dir = tmp_path / "fake-claude-calls"
    calls_dir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    for name, body in (("claude", STUB_CLAUDE), ("osascript", FAKE_OSASCRIPT)):
        _write_stub(bin_dir, name, body)
    if git := direct_git():
        (bin_dir / "git").symlink_to(git)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_CLAUDE_DIR", str(calls_dir))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGENT_NOTIFY", "off")
    monkeypatch.delenv("AGENT_DISPATCH_ID", raising=False)
    fake = FakeClaude(calls_dir, home)
    # patched on the submodule that owns and reads each name: the package re-export is a
    # separate binding the submodule never looks at (src/agent_cli/dispatch/__init__.py)
    monkeypatch.setattr(dispatch_mod.supervisor, "transport_factory", fake.factory)
    # a 0.2s poll makes every steer/timeout/watchdog test wait a fifth of a second per tick
    monkeypatch.setattr(dispatch_mod.state, "POLL_SECONDS", 0.02)
    # every worker holds open after a `question` now, waiting for an answer on the inbox
    # (it used to be one repo's stream-mode behaviour only). Ten real minutes per question test is
    # not a suite. The tests that care about the hold itself raise this themselves.
    monkeypatch.setattr(dispatch_mod.state, "QUESTION_IDLE_SECONDS", 0.2)
    monkeypatch.setattr(
        dispatch_mod.notify_mod,
        "_macos_notify",
        lambda title, text: fake.notifications.append(("macos", title, text)) or "sent",
    )
    return fake


class Result:
    def __init__(self, code: int, out: str, err: str):
        self.code = code
        self.out = out
        self.err = err

    @property
    def json(self):
        return json.loads(self.out)


@pytest.fixture
def run():
    """Invoke the CLI in-process: run(["feature", "new", ...]) -> Result."""

    def _run(argv: list[str], stdin: str | None = None) -> Result:
        out, err = io.StringIO(), io.StringIO()
        old_stdin = sys.stdin
        if stdin is not None:
            sys.stdin = io.StringIO(stdin)
        try:
            with redirect_stdout(out), redirect_stderr(err):
                try:
                    code = main(argv)
                except SystemExit as exc:  # argparse usage errors
                    code = int(exc.code or 0)
        finally:
            sys.stdin = old_stdin
        return Result(code, out.getvalue(), err.getvalue())

    return _run
