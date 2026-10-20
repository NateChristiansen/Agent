"""Codex candidate runtime and limits (phase B).

- Candidate identity: a Codex worker's commands import its own checkout, the policy hook imports
  the trusted harness, and the suite refuses to run against another tree (origin_guard.py).
- Budget: the cap is the total across attempts ($0.90 spent, $1.00 cap, $0.20 more must stop).
- `max_tool_items`: Codex's own limit, counted by item id; `max_turns` is shown as not applicable.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import subprocess
import venv
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

import origin_guard
from agent_cli import prices, repos
from agent_cli.dispatch import codex_transport, spend, state
from agent_cli.dispatch.codex_permissions import hook_config
from agent_cli.dispatch.evaluate import evaluate
from agent_cli.dispatch.options import CODEX_TURN_NOTE, DEFAULT_MAX_TOOL_ITEMS, codex_options_preview
from dispatch_helpers import _new_fast, droot  # noqa: F401 -- pytest fixture

ROOT = Path(__file__).resolve().parents[1]


def _fake_checkout(where: Path) -> Path:
    """A tree whose `agent_cli` would announce itself if anything imported it."""
    marker = where / "imported-the-fake"
    pkg = where / "src" / "agent_cli"
    (pkg / "dispatch").mkdir(parents=True)
    plant = f"open({str(marker)!r}, 'w').write('yes')\n"
    (pkg / "__init__.py").write_text(plant, encoding="utf-8")
    (pkg / "dispatch" / "__init__.py").write_text(plant, encoding="utf-8")
    (pkg / "dispatch" / "codex_permissions.py").write_text(plant, encoding="utf-8")
    for top in (where / "agent_cli", where / "agent_cli" / "dispatch"):  # a cwd-relative shadow too
        top.mkdir(parents=True, exist_ok=True)
        (top / "__init__.py").write_text(plant, encoding="utf-8")
    (where / "agent_cli" / "dispatch" / "codex_permissions.py").write_text(plant, encoding="utf-8")
    return marker


# Candidate identity ------------------------------------------------------------------------------


def test_the_suite_imports_this_checkout() -> None:
    origin_guard.check(ROOT)


def test_the_guard_refuses_another_tree(tmp_path: Path) -> None:
    with pytest.raises(pytest.UsageError, match="not this checkout's"):
        origin_guard.check(tmp_path, with_child=False)


def test_a_child_with_a_foreign_pythonpath_is_caught(tmp_path: Path) -> None:
    _fake_checkout(tmp_path)
    env = {**os.environ, "PYTHONPATH": str(tmp_path / "src")}
    assert origin_guard.child(env) == (tmp_path / "src" / "agent_cli").resolve()
    assert origin_guard.child(env) != origin_guard.expected(ROOT)


def test_real_editable_venv_refuses_foreign_source_and_verifier_selects_candidate(tmp_path: Path) -> None:
    """A real editable installation is wrong even when no inherited PYTHONPATH points there."""
    uv = shutil.which("uv")
    assert uv is not None, "uv is a required harness tool"
    foreign = tmp_path / "foreign"
    package = foreign / "src" / "agent_cli"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("# Foreign candidate fixture\n", encoding="utf-8")
    (foreign / "pyproject.toml").write_text(
        '[build-system]\nrequires = []\nbuild-backend = "editable_fixture"\nbackend-path = ["."]\n',
        encoding="utf-8",
    )
    # A dependency-free PEP 660 backend lets the offline fixture make a real editable install.
    (foreign / "editable_fixture.py").write_text(
        """from pathlib import Path
import zipfile
def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    name = "foreign_agent_fixture-0.0.0-py3-none-any.whl"
    dist = "foreign_agent_fixture-0.0.0.dist-info/"
    files = {
        "foreign_agent_fixture.pth": str(Path(__file__).parent / "src") + "\\n",
        dist + "METADATA": "Metadata-Version: 2.1\\nName: foreign-agent-fixture\\nVersion: 0.0.0\\n",
        dist + "WHEEL": "Wheel-Version: 1.0\\nGenerator: fixture\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n",
    }
    files[dist + "RECORD"] = "".join(path + ",,\\n" for path in [*files, dist + "RECORD"])
    with zipfile.ZipFile(Path(wheel_directory) / name, "w") as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return name
""",
        encoding="utf-8",
    )
    environment = tmp_path / "shared-venv"
    venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    env = {k: value for k, value in os.environ.items() if k != "PYTHONPATH"}
    env.update(UV_CACHE_DIR=str(tmp_path / "uv-cache"), UV_OFFLINE="1", UV_PYTHON_DOWNLOADS="never")
    installed = subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--offline",
            "--no-deps",
            "--no-build-isolation",
            "--python",
            str(python),
            "--editable",
            str(foreign),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert installed.returncode == 0, installed.stderr
    found = subprocess.run(
        [str(python), "-c", origin_guard.PROBE], env=env, capture_output=True, text=True, check=True
    )
    assert Path(found.stdout).parent == package
    candidate = tmp_path / "candidate"
    (candidate / "src/agent_cli").mkdir(parents=True)
    (candidate / "src/agent_cli/__init__.py").write_text("# Reviewed candidate\n", encoding="utf-8")
    (candidate / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (candidate / "conftest.py").write_text(
        f"import sys\nsys.path.insert(0, {str(ROOT / 'tests')!r})\nimport origin_guard\n"
        "def pytest_configure(config):\n    origin_guard.check(config.rootpath)\n",
        encoding="utf-8",
    )
    (candidate / "test_candidate.py").write_text("def test_candidate():\n    assert True\n", encoding="utf-8")
    main_dependencies = str(Path(pytest.__file__).resolve().parent.parent)
    runner = f"import sys;sys.path.append({main_dependencies!r});import pytest;raise SystemExit(pytest.main(['-q', {str(candidate)!r}]))"
    refused = subprocess.run(
        [str(python), "-c", runner], cwd=candidate, env=env, capture_output=True, text=True, timeout=30
    )
    assert refused.returncode == 4, refused.stdout + refused.stderr
    assert str(package) in refused.stderr and "not this checkout's" in refused.stderr
    # The verifier's real isolated process launcher receives an explicit candidate source.
    from agent_cli.dispatch.verification_process import capture

    candidate_env = {**env, "PYTHONPATH": str(candidate / "src")}
    code, output = capture([str(python), "-c", origin_guard.PROBE], candidate, candidate_env, 10)
    assert code == 0 and Path(output.decode()).parent == candidate / "src/agent_cli"
    accepted = subprocess.run(
        [str(python), "-c", runner],
        cwd=candidate,
        env=candidate_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr


@pytest.mark.parametrize("product_has_agent_cli", [True, False])
def test_the_policy_hook_imports_the_trusted_harness(tmp_path: Path, product_has_agent_cli: bool) -> None:
    marker = _fake_checkout(tmp_path) if product_has_agent_cli else tmp_path / "imported-the-fake"
    command = hook_config()["PreToolUse"][0]["hooks"][0]["command"]
    env = {**os.environ, "PYTHONPATH": str(tmp_path / "src"), "PYTHONSAFEPATH": ""}
    done = subprocess.run(
        shlex.split(command, posix=os.name != "nt"),
        input="{}",
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
        timeout=60,
    )
    assert not marker.exists()
    reply = json.loads(done.stdout)
    assert reply["hookSpecificOutput"]["hookEventName"] == "PreToolUse"


class _RPC:
    """A Codex app-server double: answers thread and turn calls, then replays `events`."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.process = None
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        for event in events:
            self.events.put_nowait(event)
        self.calls: list[str] = []

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(method)
        if method.startswith("thread/"):
            return {"thread": {"id": "thread-1"}}
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        return {}

    def stderr_tail(self) -> str:
        return ""


def _run(
    droot: Path, monkeypatch: pytest.MonkeyPatch, d: dict[str, Any], events: list[dict[str, Any]]
) -> tuple[dict[str, Any], _RPC, dict[str, str]]:
    d["model"] = "gpt-6-luna"
    repo = repos.load_registry(droot).require("alpha")
    rpc = _RPC(events)
    seen: dict[str, str] = {}

    @asynccontextmanager
    async def isolated(*_args: Any, **kwargs: Any) -> AsyncIterator[tuple[_RPC, dict[str, Any]]]:
        seen.update(kwargs["env"])
        yield rpc, {}

    monkeypatch.setattr(codex_transport, "isolated_server", isolated)
    monkeypatch.setattr(prices, "price_usage", lambda _model, _usage: 0.20)
    state.save(droot, d)
    result = asyncio.run(
        codex_transport._run_codex(
            droot,
            d,
            repo,
            text="probe",
            resume=False,
            mcp_servers={},
            inbox=droot / ".agent/scratch/absent-inbox",
            timeout=30,
        )
    )
    return result, rpc, seen


def _usage(total: int) -> dict[str, Any]:
    last = {"inputTokens": 10, "outputTokens": 5, "cachedInputTokens": 0}
    return {
        "method": "thread/tokenUsage/updated",
        "params": {"tokenUsage": {"last": last, "total": {"totalTokens": total}}},
    }


def _item(kind: str, item_id: str, **extra: Any) -> dict[str, Any]:
    return {"method": "item/completed", "params": {"item": {"type": kind, "id": item_id, **extra}}}


FINAL = _item("agentMessage", "final", phase="final_answer", text='{"status": "done", "summary": "ok"}')
COMPLETED = {"method": "turn/completed", "params": {"turn": {"status": "completed"}}}


def test_the_worker_environment_has_no_manager_pythonpath(
    droot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = _new_fast(droot)
    result, _rpc, env = _run(droot, monkeypatch, d, [FINAL, COMPLETED])
    assert result["stop_reason"] == "result"
    assert "PYTHONPATH" not in env


# Budget across attempts ----------------------------------------------------------------------------


def test_the_cap_counts_what_earlier_attempts_spent(droot: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = _new_fast(droot)
    d.update(cost_usd=0.90, budget_usd=1.00, cost_accounting=spend.N1)
    result, rpc, _env = _run(droot, monkeypatch, d, [_usage(100), FINAL, COMPLETED])
    assert result["stop_reason"] == "budget_exhausted"
    assert "turn/interrupt" in rpc.calls
    limits = result["last_result"]["codex_limits"]
    assert limits["budget_spent_before_usd"] == pytest.approx(0.90)
    assert limits["budget_basis"] == spend.N1


def test_a_duplicate_usage_event_is_priced_once(droot: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = _new_fast(droot)
    d.update(cost_usd=0.50, budget_usd=1.00, cost_accounting=spend.N1)
    result, _rpc, _env = _run(droot, monkeypatch, d, [_usage(100), _usage(100), FINAL, COMPLETED])
    assert result["stop_reason"] == "result"
    assert result["estimated_cost_usd"] == pytest.approx(0.20)


def test_an_unknown_cost_history_counts_nothing_earlier_and_says_so(
    droot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = _new_fast(droot)
    d.update(cost_usd=5.00, budget_usd=1.00, cost_accounting=spend.UNRESOLVED)
    result, _rpc, _env = _run(droot, monkeypatch, d, [_usage(100), FINAL, COMPLETED])
    assert result["stop_reason"] == "result"
    assert result["last_result"]["codex_limits"]["budget_basis"].startswith("unknown")


def test_a_resume_with_no_new_generation_spends_nothing(droot: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = _new_fast(droot)
    d.update(cost_usd=0.99, budget_usd=1.00, cost_accounting=spend.N1)
    result, _rpc, _env = _run(droot, monkeypatch, d, [FINAL, COMPLETED])
    assert result["stop_reason"] == "result"
    assert result["estimated_cost_usd"] == 0


# Tool-item limit ------------------------------------------------------------------------------------


def test_parallel_and_duplicate_tool_items_count_by_id(droot: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = _new_fast(droot)
    d["max_tool_items"] = 3
    events = [
        _item("commandExecution", "c1"),
        _item("fileChange", "f1"),  # parallel with c1
        _item("commandExecution", "c1"),  # a repeated completion event
        _item("reasoning", "r1"),
        _item("agentMessage", "m1", phase="commentary", text="still working"),
        FINAL,
        COMPLETED,
    ]
    result, rpc, _env = _run(droot, monkeypatch, d, events)
    assert result["stop_reason"] == "result"
    assert "turn/interrupt" not in rpc.calls
    limits = result["last_result"]["codex_limits"]
    assert (limits["tool_items"], limits["max_tool_items"], limits["agent_messages"]) == (2, 3, 2)
    assert limits["protocol_turns"] == 1


def test_message_only_progress_is_not_a_tool_item(droot: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = _new_fast(droot)
    d["max_tool_items"] = 1
    chatter = [_item("agentMessage", f"m{i}", phase="commentary", text="thinking") for i in range(20)]
    result, _rpc, _env = _run(droot, monkeypatch, d, [*chatter, FINAL, COMPLETED])
    assert result["stop_reason"] == "result"
    assert result["last_result"]["codex_limits"]["tool_items"] == 0


def test_a_low_cap_stops_with_max_tool_items_and_blocks(droot: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = _new_fast(droot)
    d["max_tool_items"] = 2
    d["options"] = {"max_tool_items": 2}
    events = [_item("commandExecution", f"c{i}") for i in range(5)] + [FINAL, COMPLETED]
    result, rpc, _env = _run(droot, monkeypatch, d, events)
    assert result["stop_reason"] == "max_tool_items"
    assert "turn/interrupt" in rpc.calls
    assert result["last_result"]["terminal_reason"] == "max_tool_items"
    d = state.load(droot, d["id"])
    d.update(result)
    verdict = evaluate(droot, d)
    assert verdict["state"] == "blocked"
    assert "Codex tool-item cap 2 reached" in verdict["error"]


def test_a_default_codex_launch_completes_under_the_default_cap(
    droot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = _new_fast(droot)
    events = [_item("commandExecution", f"c{i}") for i in range(170)] + [FINAL, COMPLETED]
    result, _rpc, _env = _run(droot, monkeypatch, d, events)
    assert result["stop_reason"] == "result"
    limits = result["last_result"]["codex_limits"]
    assert limits == {**limits, "tool_items": 170, "max_tool_items": DEFAULT_MAX_TOOL_ITEMS}


def test_the_preview_shows_max_turns_as_not_applicable(droot: Path) -> None:
    d = _new_fast(droot)
    repo = repos.load_registry(droot).require("alpha")
    preview = codex_options_preview(d, repo, resume=False)
    assert preview["max_turns"] == CODEX_TURN_NOTE
    assert preview["max_tool_items"] == DEFAULT_MAX_TOOL_ITEMS
    assert "timeout" in preview["message_only_progress"]


QUESTION = _item(
    "agentMessage", "q", phase="final_answer", text='{"status": "question", "question": "which file?"}'
)


@pytest.mark.parametrize("cap", ["max_tool_items", "budget_exhausted"])
def test_a_cap_after_an_answered_question_reports_the_second_turn(
    droot: Path, monkeypatch: pytest.MonkeyPatch, cap: str
) -> None:
    """Codex's phase-B review: the answered question's result stood in for the capped turn."""
    from agent_cli.dispatch import notice_producers

    d = _new_fast(droot)
    d.update(model="gpt-6-luna", cost_usd=0.05, budget_usd=0.35, cost_accounting=spend.N1, max_tool_items=2)
    inbox = droot / ".agent/scratch/inbox-q"
    inbox.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        notice_producers, "question_hold", lambda *_a: inbox.write_text("cost.py", encoding="utf-8")
    )
    monkeypatch.setattr(notice_producers, "answered", lambda *_a: None)
    second = (
        [_item("commandExecution", f"c{i}") for i in range(3)] if cap == "max_tool_items" else [_usage(200)]
    )
    events = [_usage(100), QUESTION, COMPLETED, *second, FINAL, COMPLETED]
    repo = repos.load_registry(droot).require("alpha")
    rpc = _RPC(events)

    @asynccontextmanager
    async def isolated(*_args: Any, **_kwargs: Any) -> AsyncIterator[tuple[_RPC, dict[str, Any]]]:
        yield rpc, {}

    monkeypatch.setattr(codex_transport, "isolated_server", isolated)
    monkeypatch.setattr(prices, "price_usage", lambda _model, _usage: 0.20)
    state.save(droot, d)
    result = asyncio.run(
        codex_transport._run_codex(
            droot, d, repo, text="probe", resume=False, mcp_servers={}, inbox=inbox, timeout=30
        )
    )
    assert result["stop_reason"] == cap
    envelope = result["last_result"]
    assert envelope["terminal_reason"] == cap
    assert envelope["num_turns"] == 2
    assert envelope["total_cost_usd"] == pytest.approx(0.40 if cap == "budget_exhausted" else 0.20)
