"""Dispatch CLI version attribution at launch, across resumes and on historical records."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agent_cli import dispatch, ops
from agent_cli.core import agent_path
from agent_cli.dispatch import client_version, codex_policy, codex_transport, supervisor
from dispatch_helpers import _new_fast, droot, envelope, one  # noqa: F401 -- pytest fixture


@pytest.fixture(autouse=True)
def _clear_versions() -> Iterator[None]:
    probe = client_version._probe
    probe.cache_clear()
    yield
    probe.cache_clear()


@pytest.mark.parametrize(
    "provider,version", [("claude", "2.1.302 (Claude Code)"), ("codex", "codex-cli 0.163.0")]
)
def test_launch_and_resume_keep_each_attempt_version(droot: Path, monkeypatch, run, provider, version):
    probes: list[tuple[str, ...]] = []
    seen: list[dict[str, Any]] = []

    def probe(command: tuple[str, ...], *, root: Path | None = None) -> str:
        assert root == droot
        probes.append(command)
        return version if len(probes) == 1 else version + "-updated"

    def worker(root: Path, d: dict[str, Any], *args: Any, **kwargs: Any) -> dict[str, Any]:
        stored = dispatch.load(root, d["id"])
        assert str(d["attempt"]) in stored["client_versions"], "written before worker launch"
        seen.append(stored["client_versions"])
        final = envelope("question", nonce=d["nonce"], open_questions=["continue?"])
        final["session_id"] = d["session_id"]
        return {
            "last_result": final,
            "session_established": True,
            "exit_code": 0,
            "timed_out": False,
            "stop_reason": "result",
        }

    monkeypatch.setattr(client_version, "probe", probe)
    monkeypatch.setattr(supervisor, "run_worker", worker)
    monkeypatch.setattr(codex_transport, "run_codex", worker)
    # No installed Codex is needed; policy and version probing share this resolved prefix.
    monkeypatch.setattr(codex_policy, "command", lambda *args: ["/tools/codex", *args])
    d = _new_fast(droot, provider=provider, row="build", budget_reason="version probe")
    first = {"client": provider, "command": list(probes[0]), "version": version}
    assert d["client_versions"] == {"1": first}
    assert probes[0] == (("/tools/codex",) if provider == "codex" else (d["options"]["cli_path"],))
    resumed = dispatch.resume(droot, dispatch_id=d["id"], answer="continue", watch=True)
    assert resumed["client_versions"] == {
        "1": first,
        "2": {**first, "version": version + "-updated"},
    }
    assert len(seen) == 2
    shown = ops.dispatch_show(droot, dispatch_id=d["id"], consume=False)
    assert shown.data["client_versions"] == resumed["client_versions"]
    for attempt, suffix in [(1, ""), (2, "-updated")]:
        assert f"client (attempt {attempt}): {provider} {version}{suffix}" in shown.text
    assert f"client (attempt 2): {provider} {version}-updated" in run(["dispatch", "show", d["id"]]).out


def test_old_record_without_client_versions_still_loads_and_shows(droot: Path, monkeypatch):
    monkeypatch.setattr(supervisor, "popen", type("P", (), {"pid": 1, "__init__": lambda *a, **k: None}))
    d = _new_fast(droot, watch=False)
    assert "client_versions" not in dispatch.load(droot, d["id"])
    shown = ops.dispatch_show(droot, dispatch_id=d["id"], consume=False)
    assert shown.data["id"] == d["id"]
    assert "client (attempt" not in shown.text


def test_unknown_version_does_not_block_launch(droot: Path, fake_claude, monkeypatch):
    monkeypatch.setattr(client_version, "probe", lambda command, **kwargs: None)
    fake_claude.scenario([one(envelope())])
    d = _new_fast(droot)
    assert d["state"] == "done"
    assert d["client_versions"]["1"]["version"] is None
    assert (
        "client (attempt 1): claude (version unknown)"
        in ops.dispatch_show(droot, dispatch_id=d["id"], consume=False).text
    )


def test_probe_uses_version_flag_and_caches_per_command(monkeypatch):
    calls: list[tuple[str, ...]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(tuple(command))
        assert kwargs["timeout"] == 5
        assert kwargs["stdin"] == subprocess.DEVNULL
        return subprocess.CompletedProcess(command, 0, "\n codex-cli 0.163.0\n")

    monkeypatch.setattr(client_version.subprocess, "run", run)
    for command in [("/tools/codex",), ("node.exe", "codex.js")]:
        assert client_version.probe(command) == "codex-cli 0.163.0"
        assert client_version.probe(command) == "codex-cli 0.163.0"
    assert calls == [("/tools/codex", "--version"), ("node.exe", "codex.js", "--version")]


@pytest.mark.parametrize("failure", ["missing", "timeout", "exit", "empty"])
def test_probe_failure_is_unknown_and_cached(monkeypatch, failure):
    calls = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if failure == "missing":
            raise FileNotFoundError
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 5)
        return subprocess.CompletedProcess(
            command, 1 if failure == "exit" else 0, "" if failure == "empty" else "diagnostic"
        )

    monkeypatch.setattr(client_version.subprocess, "run", run)
    assert client_version.probe(("/tools/claude",)) is None
    assert client_version.probe(("/tools/claude",)) is None
    assert len(calls) == 1


def test_codex_node_wrapper_is_preserved_and_server_flags_are_removed(monkeypatch):
    seen = []
    monkeypatch.setattr(
        client_version, "probe", lambda command, **kwargs: seen.append(command) or "codex-cli 0.163.0"
    )
    record = client_version.record(
        "codex", {"command": ["node.exe", "codex.js", "app-server", "--stdio", "-c", "x=y"]}
    )
    assert seen == [("node.exe", "codex.js")]
    assert record == {"client": "codex", "command": ["node.exe", "codex.js"], "version": "codex-cli 0.163.0"}


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_missing_executable_has_unknown_version(provider):
    assert client_version.record(provider, {}) == {"client": provider, "command": [], "version": None}


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_fresh_supervisors_reuse_the_installed_version(tmp_path: Path, provider):
    script = tmp_path / "client.py"
    counter = tmp_path / "probes.txt"
    script.write_text("print('client 1.0')\n", encoding="utf-8")
    command = [sys.executable, str(script)]
    # Separate interpreters exercise the supervisor's process lifetime, with no live harness.
    code = (
        "import json, subprocess, sys\n"
        "from pathlib import Path\n"
        "from agent_cli.dispatch.client_version import record\n"
        "run = subprocess.run\n"
        "def counted_run(argv, **kwargs):\n"
        "    if argv[-1] == '--version':\n"
        "        with Path(sys.argv[4]).open('a') as f: f.write('probe\\n')\n"
        "    return run(argv, **kwargs)\n"
        "subprocess.run = counted_run\n"
        "command = json.loads(sys.argv[2])\n"
        "options = {'command': [*command, 'app-server', '--stdio']}\n"
        "if sys.argv[3] == 'claude':\n"
        "    options = {'cli_path': command[0]}\n"
        "print(json.dumps(record(sys.argv[3], options, root=Path(sys.argv[1]))))\n"
    )
    # Claude has a single executable prefix; use Python's own version for that control.
    results = []
    for _ in range(2):
        done = subprocess.run(
            [sys.executable, "-c", code, str(tmp_path), json.dumps(command), provider, str(counter)],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        results.append(json.loads(done.stdout))
    assert results[0] == results[1]
    assert results[0]["version"]
    assert counter.read_text(encoding="utf-8").splitlines() == ["probe"]
    if provider == "claude":
        assert results[0]["version"].startswith("Python ")


@pytest.mark.parametrize("change", ["mtime", "size", "realpath", "wrapper"])
def test_install_changes_invalidate_both_caches(tmp_path: Path, monkeypatch, change):
    executable = tmp_path / "client"
    executable.write_text("v1", encoding="utf-8")
    wrapper = tmp_path / "client.js"
    wrapper.write_text("v1", encoding="utf-8")
    command = (str(executable), str(wrapper)) if change == "wrapper" else (str(executable),)
    calls = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, f"client {len(calls)}\n")

    monkeypatch.setattr(client_version.subprocess, "run", run)
    assert client_version.probe(command, root=tmp_path) == "client 1"
    assert client_version.probe(command, root=tmp_path) == "client 1"
    changed = wrapper if change == "wrapper" else executable
    info = changed.stat()
    if change == "mtime":
        os.utime(changed, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
    elif change in {"size", "wrapper"}:
        changed.write_text("version two", encoding="utf-8")
        os.utime(changed, ns=(info.st_atime_ns, info.st_mtime_ns))
    else:
        replacement = tmp_path / "other-client"
        replacement.write_text("v1", encoding="utf-8")
        os.utime(replacement, ns=(info.st_atime_ns, info.st_mtime_ns))
        command = (str(replacement),)
    assert client_version.probe(command, root=tmp_path) == "client 2"
    client_version._probe.cache_clear()
    assert client_version.probe(command, root=tmp_path) == "client 2"
    assert len(calls) == 2


@pytest.mark.parametrize("contents", ["{", "[]", '{"bad": 42}', b"\xff"])
def test_corrupt_cache_reprobes_and_recovers(tmp_path: Path, monkeypatch, contents):
    path = agent_path(tmp_path, "dispatches", "cache", "client-versions.json")
    path.parent.mkdir(parents=True)
    path.write_bytes(contents.encode("utf-8") if isinstance(contents, str) else contents)
    calls = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "client recovered\n")

    monkeypatch.setattr(client_version.subprocess, "run", run)
    command = (sys.executable,)
    assert client_version.probe(command, root=tmp_path) == "client recovered"
    assert list(json.loads(path.read_text(encoding="utf-8")).values()) == ["client recovered"]
    client_version._probe.cache_clear()
    assert client_version.probe(command, root=tmp_path) == "client recovered"
    assert len(calls) == 1


@pytest.mark.parametrize("operation", ["read", "write"])
def test_cache_io_failure_does_not_hide_a_probed_version(tmp_path: Path, monkeypatch, operation):
    path = agent_path(tmp_path, "dispatches", "cache", "client-versions.json")
    real_read = Path.read_text

    def read(file: Path, *args: Any, **kwargs: Any) -> str:
        if file == path:
            raise PermissionError("cache unreadable")
        return real_read(file, *args, **kwargs)

    def write(*args: Any, **kwargs: Any) -> None:
        raise PermissionError("cache unwritable")

    if operation == "read":
        monkeypatch.setattr(Path, "read_text", read)
    else:
        monkeypatch.setattr(client_version, "atomic_write", write)
    monkeypatch.setattr(
        client_version.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "client 1\n"),
    )
    assert client_version.probe((sys.executable,), root=tmp_path) == "client 1"
    if operation == "read":
        assert list(json.loads(real_read(path, encoding="utf-8")).values()) == ["client 1"]


def test_cache_replacement_is_atomic(tmp_path: Path, monkeypatch):
    from agent_cli import core

    path = agent_path(tmp_path, "dispatches", "cache", "client-versions.json")
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")
    real_replace = core.replace
    replacements = []

    def replace(source, destination):
        assert Path(source).parent == path.parent
        assert destination == path
        assert path.read_text(encoding="utf-8") == "{}"
        assert list(json.loads(Path(source).read_text(encoding="utf-8")).values()) == ["client 1"]
        replacements.append(destination)
        real_replace(source, destination)

    monkeypatch.setattr(core, "replace", replace)
    monkeypatch.setattr(
        client_version.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "client 1\n"),
    )
    assert client_version.probe((sys.executable,), root=tmp_path) == "client 1"
    assert replacements == [path]
    assert list(path.parent.iterdir()) == [path]
