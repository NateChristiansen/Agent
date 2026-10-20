"""Replay turn endings, ownership switches and owner-facing links."""

from __future__ import annotations

import importlib
import json
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from agent_cli import core, features, log, peers, sessions
from agent_cli.core import AgentError
from agent_cli.dispatch import state
from agent_cli.hooks import ask_guard, groups
from agent_cli.hooks.task_waits import wait_count
from agent_cli.mcp import specs_features, specs_harness
from agent_cli.mcp.tools import Ctx
from agent_cli.ops import features as feature_ops
from agent_cli.ops import notes as notes_ops
from agent_cli.ops import peers as peer_ops
from agent_cli.ops.sessions import owner_question
from test_attention_matrix import SID, manager  # noqa: F401 (fixture)

run = importlib.import_module("agent_cli.hooks.run")
ROOT = Path(__file__).parents[1]
W1 = (
    "I've taken sole lead of `045`, mapped this session, and published the takeover record. "
    "You can close the Mac manager.\n\nOnly the Windows milestone remains. "
    "I'm reconciling prior attempt usage before running the authorized pair."
)


def prompt(root, text="continue"):
    ask_guard.ask_guard(root, {"session_id": SID, "hook_event_name": "UserPromptSubmit", "prompt": text})


def stop(root, text, **extra):
    return ask_guard.ask_guard(
        root, {"session_id": SID, "hook_event_name": "Stop", "last_assistant_message": text, **extra}
    )


@pytest.mark.parametrize("client,group", [("codex", "codex-stop"), ("claude", "stop")])
def test_w1_replay_with_peer_send_publication_and_a4(manager, notes_remote, client, group):
    sessions.set_field(manager, SID, "provider", client)
    prompt(manager)
    sessions.set_record(manager, "peer-9727", name="cx 9727", pid=4242)
    peer_ops.peer_send(
        manager,
        session_id=SID,
        to="cx 9727",
        message="Which attempts remain unused? Launch nothing more.",
        expect_reply=True,
    )
    notes_ops.notes_publish(manager)
    assert wait_count(sessions.get(manager, SID) or {}) == 1
    payload = {"session_id": SID, "hook_event_name": "Stop", "last_assistant_message": W1}
    assert "Your reply announced work" in run.run_group(manager, groups.GROUPS[group], payload)
    payload["last_assistant_message"] = "I'll read the usage records next."
    assert run.run_group(manager, groups.GROUPS[group], payload) == ""
    prompt(manager)
    assert "Your reply announced work" in stop(manager, W1)


ANNOUNCEMENTS = [
    "I'm reconciling the allowance.",
    "I’m checking the log.",
    "I'll read the log.",
    "Next I read the log.",
    "Next I'll check the result.",
    "Waiting for the build. I'll check the log in the meantime.",
    "I'm waiting for the build, and I'll check the usage records.",
]


def test_announcements_do_not_gain_background_or_peer_exemptions(manager):
    for text in ANNOUNCEMENTS:
        prompt(manager)
        peers.begin_wait(manager, SID, "peer", 600)
        sessions.set_field(manager, SID, "background_tasks", [{"id": "build", "status": "running"}])
        assert json.loads(stop(manager, text))["decision"] == "block", text
        assert stop(manager, "I'll continue checking.", turn_id="changed", stop_hook_active=True) == "", text


EXEMPT_ENDINGS = [
    "Waiting on cx 9727's reply.",
    "I'm waiting for the build.",
    "I'll wait for the build.",
    "The reconciliation is complete.",
    "The worker is checking the log.",
    "> I'll read the log.",
    "`I'll read the log.`",
    'The worker said "I\'ll read the log."',
    "I'll review it when it returns.",
    "Next I'll review 001-mac-18 once it returns.",
    "I'll report back when the worker finishes.",
    "I'll check the log after the worker replies.",
    "I'll leave 046 parked until you decide.",
    "I'll review it if the worker returns.",
    "When the worker returns, I'll review it.",
    "I'll keep an eye on it.",
    "Landed `abc1234`. I'm leaving 046 parked until you decide. All 40 tests pass.",
    "Done. I'll pick up task 4 when you say go.",
    "Dispatched 001-mac-18. I'll review it as soon as it returns.",
    "Done. I'll land it as soon as you say go.",
    "Plan is in plan.md. I'll dispatch task 4 on your go.",
    "Landed. I'll leave it there.",
    "The worker is blocked on the fixture; I'm leaving it parked.",
    "Nothing left to do; I'll stand by.",
    "I'll review it whenever it returns.",
    "I'll review it on return.",
    "I'll review it the moment it lands.",
    "Give me the go and I'll dispatch it.",
    "Say go and I'll dispatch it.",
    "I'll stand by for your go.",
    "I'll stop here.",
    "I'll leave the rest for tomorrow.",
    "I'm keeping the mapping on 009.",
    "I'll review it as soon as it returns.",
    "I'll dispatch task 4 on your go.",
    "I'll leave it there.",
    "I'm leaving it parked.",
    "I'll stand by.",
    "I'll dispatch it on your approval.",
    "I'll review it on your return.",
    "I'll dispatch it on your word.",
    "I'll dispatch it on go.",
    "I'll dispatch it on approval.",
    "I'll dispatch it on word.",
    "Say the go and I'll dispatch it.",
    "Give me go and I'll dispatch it.",
    "I'll keep the mapping on 009.",
    "I'll park it.",
    "I'm parking it.",
    "I'm stopping here.",
    "I am leaving it parked.",
    "I'm standing by.",
    "Next I stop here.",
]


def test_dependency_and_completed_endings_are_exempt(manager):
    for text in EXEMPT_ENDINGS:
        prompt(manager)
        peers.begin_wait(manager, SID, "peer", 600)
        assert stop(manager, text) == "", text


def test_question_does_not_spend_announcement_cap(manager):
    prompt(manager)
    owner_question(manager, session_id=SID, summary="Should I push main?")
    assert stop(manager, "I'll publish after approval. Should I push main?") == ""
    owner_question(manager, session_id=SID, resolution="answered")
    assert "Your reply announced work" in stop(manager, "I'll read the log.")


def test_announcement_nudge_preserves_unrecognised_dependency(manager):
    prompt(manager)
    reason = json.loads(stop(manager, "I'll dispatch it at your signal."))["reason"]
    assert "without the owner or another agent" in reason
    assert "waits on the owner, a worker or a peer, don't start it" in reason
    assert "end your turn with one line naming what it waits on" in reason
    assert stop(manager, "I'll read the log now.") == ""


@pytest.mark.parametrize("text", ["Done. I'll pick up task 4 when you say go.", "I'll read the log."])
@pytest.mark.parametrize("client,group", [("codex", "codex-stop"), ("claude", "stop")])
def test_pending_question_this_turn_exempts_announcement_without_spending_cap(manager, text, client, group):
    sessions.set_field(manager, SID, "provider", client)
    prompt(manager)
    owner_question(manager, session_id=SID, summary="Start task 4?")
    payload = {"session_id": SID, "hook_event_name": "Stop", "last_assistant_message": text}
    assert "Your reply announced work" not in run.run_group(manager, groups.GROUPS[group], payload)
    assert (sessions.get(manager, SID) or {})["owner_question_pending"]
    owner_question(manager, session_id=SID, resolution="answered")
    assert "Your reply announced work" in stop(manager, "I'll read the log.")


def test_pending_question_from_an_earlier_turn_does_not_cover_announcement(manager):
    prompt(manager)
    owner_question(manager, session_id=SID, summary="Start task 4?")
    prompt(manager)
    assert "Your reply announced work" in stop(manager, "I'll read the log.")


def test_announcement_before_closing_paragraph_is_exempt(manager):
    for text in [
        "I'll read the log.\n\nAll 40 tests pass.",
        "I'm keeping the mapping on 009.\n\nThe 062 worker is blocked on a missing fixture.",
        "Summary: I'm attaching the evidence below.\n\n- tests pass\n- lint clean",
    ]:
        prompt(manager)
        assert stop(manager, text) == "", text


def test_dependency_does_not_cover_another_ungated_action(manager):
    prompt(manager)
    assert "Your reply announced work" in stop(
        manager, "I'll review it when it returns, and I'll read the usage records now."
    )


def test_short_question_ending_exempts_announcement(manager):
    prompt(manager)
    assert stop(manager, "I'll read the log. Ready?") == ""


def test_prior_stop_block_and_compaction_preserve_cap(manager):
    prompt(manager)
    assert stop(manager, W1, _stop_blocked=True) == ""
    assert "Your reply announced work" in stop(manager, W1)
    ask_guard.ask_guard(manager, {"session_id": SID, "hook_event_name": "PreCompact"})
    assert stop(manager, W1) == ""


def test_command_reference_requires_fence_even_after_registered_ask(manager):
    for text in [
        "Use the commands above.",
        "The command is in my last message.",
        "The Mac cells are waiting on your three `!` commands from my last message.",
    ]:
        prompt(manager)
        owner_question(manager, session_id=SID, summary=text)
        assert "complete commands" in stop(manager, text), text
        assert stop(manager, text) == "", text
        prompt(manager)
        owner_question(manager, session_id=SID, summary=text)
        assert stop(manager, text + "\n\n```sh\npython --version\n```") == "", text


def test_quoted_fence_does_not_supply_owner_command(manager):
    prompt(manager)
    assert "complete commands" in stop(
        manager, "Use the command above.\n\n> ```sh\n> python --version\n> ```"
    )


@pytest.fixture
def local_claims(monkeypatch):
    """`feature new` claims its id on the notes remote with a fetch and a push each time; the
    mapping tests take ids from the vault alone (the remote claim has its own tests)."""
    from agent_cli import notes_claims

    def claim(root, kind, slug, *, op=None):
        return notes_claims.Claim(kind, notes_claims._next_id(root, [], [], kind), slug, op or "local")

    monkeypatch.setattr(notes_claims, "claim", claim)


@pytest.fixture
def mapped(manager, notes_remote, local_claims):
    first = features.new(manager, "first", "First", "active", [], [], [])
    second = features.new(manager, "second", "Second", "active", [], [], [])
    feature_ops.feature_use(manager, feature=first.number, session_id=SID)
    return manager, first, second


def _put(root: Path, record: dict) -> None:
    """Replace dispatch `001-01` whole, so each case sees only its own record."""
    if state.file(root, record["id"], ".json").exists():
        record = {**record, "record_revision": state.load(root, record["id"])["record_revision"]}
    state.save(root, record)


def test_remap_requires_explicit_switch_and_preserves_records_on_refusal(mapped):
    root, first, second = mapped
    for work in ("queued", "running", "owed-verdict", "adopted-verdict"):
        _put(
            root,
            {
                "id": "001-01",
                "feature": first.id,
                "launched_by_session": "ended-launcher" if work == "adopted-verdict" else SID,
                "state": work if work in ("queued", "running") else "done",
                "supervisor_pid": 4242,
                "chat_pending": True,
            },
        )
        before = sessions.get(root, SID)
        history = [f.log_path.read_text() for f in (first, second)]
        with pytest.raises(AgentError, match="switch=true"):
            feature_ops.feature_use(root, feature=second.number, session_id=SID)
        assert sessions.get(root, SID) == before, work
        assert [f.log_path.read_text() for f in (first, second)] == history, work
        feature_ops.feature_use(root, feature=second.number, session_id=SID, switch=True)
        assert (sessions.get(root, SID) or {})["feature"]["id"] == second.id, work
        for feature in (first, second):
            assert "mapped from `001` to `002`" in log.parse(feature.log_path)[-1].text, work
        feature_ops.feature_use(root, feature=first.number, session_id=SID, switch=True)


def test_plan_tasks_and_dispatches_without_open_work_do_not_hold_mapping(mapped):
    root, first, second = mapped
    for work in ("open-task", "ticked-today", "dead-queued", "dead-running", "read-verdict"):
        if work in ("open-task", "ticked-today"):
            index = feature_ops.task_add(
                root, feature=first.number, session_id=SID, text="Read usage records"
            )
            if work == "ticked-today":
                feature_ops.task_done(
                    root, feature=first.number, session_id=SID, task=str(index.data["task"])
                )
        else:
            _put(
                root,
                {
                    "id": "001-01",
                    "feature": first.id,
                    "state": work.removeprefix("dead-") if work != "read-verdict" else "done",
                    "supervisor_pid": 9999,
                    "chat_pending": work != "read-verdict",
                },
            )
        feature_ops.feature_use(root, feature=second.number, session_id=SID)
        assert (sessions.get(root, SID) or {})["feature"]["id"] == second.id, work
        feature_ops.feature_use(root, feature=first.number, session_id=SID, switch=True)


def test_closed_feature_never_requires_switch_for_open_work(mapped):
    root, first, second = mapped
    features.set_fields(root, first, status="done")
    feature_ops.task_add(root, feature=first.number, session_id=SID, text="Read usage records")
    for work in ("open-task", "queued", "running", "owed-verdict"):
        if work != "open-task":
            _put(
                root,
                {
                    "id": "001-01",
                    "feature": first.id,
                    "state": work if work in ("queued", "running") else "done",
                    "supervisor_pid": 4242,
                    "chat_pending": True,
                },
            )
        feature_ops.feature_use(root, feature=second.number, session_id=SID)
        assert (sessions.get(root, SID) or {})["feature"]["id"] == second.id, work
        feature_ops.feature_use(root, feature=first.number, session_id=SID, switch=True)


def test_first_mapping_does_not_log_a_remap(manager, notes_remote, local_claims):
    first = features.new(manager, "first", "First", "active", [], [], [])
    history = first.log_path.read_text()
    feature_ops.feature_use(manager, feature=first.number, session_id=SID)
    assert first.log_path.read_text() == history


def test_subagent_cannot_remap_even_with_explicit_switch(mapped, monkeypatch):
    root, first, second = mapped
    monkeypatch.setenv("CLAUDE_AGENT_ID", "reviewer")
    before = sessions.get(root, SID)
    history = [f.log_path.read_text() for f in (first, second)]
    with pytest.raises(AgentError, match="subagent"):
        feature_ops.feature_use(root, feature=second.number, session_id=SID, switch=True)
    assert sessions.get(root, SID) == before
    assert [f.log_path.read_text() for f in (first, second)] == history


def test_same_mapping_is_idempotent_and_inspection_does_not_remap(mapped):
    root, first, second = mapped
    feature_ops.task_add(root, feature=first.number, text="Keep working")
    history = first.log_path.read_text()
    feature_ops.feature_show(root, feature=second.number, session_id=SID)
    feature_ops.feature_use(root, feature=first.number, session_id=SID)
    assert (sessions.get(root, SID) or {})["feature"]["id"] == first.id
    assert first.log_path.read_text() == history


def test_quiet_mapping_can_switch_and_logs_both_features(mapped):
    """A task touch from yesterday does not hold the mapping either."""
    root, first, second = mapped
    yesterday = core.iso(core.now() - timedelta(days=1))
    sessions.set_field(root, SID, "feature_task_at", {str(first.id): yesterday})
    feature_ops.feature_use(root, feature=second.number, session_id=SID)
    for feature in (first, second):
        assert "mapped from `001` to `002`" in log.parse(feature.log_path)[-1].text


@pytest.mark.parametrize(
    "text",
    [
        "<cross-session-message>look at 002</cross-session-message>",
        "<agent-message>look at 002</agent-message>",
    ],
)
def test_peer_prompt_cannot_first_map_without_owner_switch(manager, notes_remote, local_claims, text):
    target = features.new(manager, "target", "Target", "active", [], [], [])
    prompt(manager, text)
    with pytest.raises(AgentError, match="owner's ask"):
        feature_ops.feature_use(manager, feature=target.number, session_id=SID)
    specs_features.feature_use(Ctx(root=manager, profile="manager"), target.number, SID, switch=True)


def test_inbox_delivery_does_not_assign_ownership_and_owner_turn_resets_context(mapped):
    root, first, second = mapped
    prompt(root)
    peers.send(root, to=SID, frm="peer-1", text="Inspect the second worker")
    peers.take(root, SID)
    with pytest.raises(AgentError, match="switch=true"):
        feature_ops.feature_use(root, feature=second.number, session_id=SID)
    prompt(root, "Take ownership of the second feature")
    feature_ops.feature_use(root, feature=second.number, session_id=SID)


def test_failed_peer_context_marker_does_not_lose_consumed_message(manager, monkeypatch):
    peers.send(manager, to=SID, frm="peer-1", text="Inspect the second worker")
    original = sessions.set_field

    def failed_marker(root, sid, field, value, timeout=None):
        if field == "feature_peer_delivered":
            raise OSError("context marker unavailable")
        return original(root, sid, field, value, timeout)

    monkeypatch.setattr(sessions, "set_field", failed_marker)
    rows = peers.take(manager, SID, timeout=1)
    assert len(rows) == 1 and rows[0]["text"] == "Inspect the second worker"


def test_link_action_encodes_path_and_configured_vault_and_refuses_non_vault_paths(manager):
    (manager / "agent.local.yaml").write_text("obsidian_vault: 'Owner Vault & Test'\n")
    for target in ["features/001-test/a b#c&d?.md", r"features\001-test\日本語.md"]:
        op = specs_harness.harness(Ctx(root=manager, profile="manager"), action="link", target=target)
        assert op.data["ok"]
        uri = urlsplit(op.data["url"])
        assert (uri.scheme, uri.netloc, uri.fragment) == ("obsidian", "open", "")
        assert parse_qs(uri.query) == {"vault": ["Owner Vault & Test"], "file": [target.replace("\\", "/")]}
    for target in ["", ".", "../outside.md", "/absolute.md", r"C:\outside.md"]:
        with pytest.raises(AgentError, match="vault-relative"):
            specs_harness.harness(Ctx(root=manager, profile="manager"), action="link", target=target)


def test_link_falls_back_to_main_vault_name(manager, monkeypatch):
    from agent_cli import config_file

    monkeypatch.setattr(config_file, "machine_root", lambda root: manager.parent / "Main Vault")
    op = specs_harness.harness(Ctx(root=manager, profile="manager"), action="link", target="Dashboard.md")
    assert parse_qs(urlsplit(op.data["url"]).query)["vault"] == ["Main Vault"]


def test_continuity_templates_end_with_first_action():
    from markdown_it import MarkdownIt

    text = (ROOT / "harness/continuity.md").read_text()
    templates = [t.content for t in MarkdownIt().parse(text) if t.type == "fence"]
    assert len(templates) == 2
    assert all(t.strip().splitlines()[-1].startswith("First action:") for t in templates)
