"""The reconciliation ledger: reserved entries, the deterministic fold, self-heal from persisted
intents, and the capture protocol with its approval hold."""

from __future__ import annotations

import itertools
import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from agent_cli import features as features_mod
from agent_cli import land_journal as jn
from agent_cli import log as log_mod
from agent_cli import reconcile
from agent_cli.core import AgentError
from agent_cli.dispatch import state
from agent_cli.log import Entry

pytestmark = pytest.mark.usefixtures("notes_remote")

AT = "2026-10-22T09:00:00+00:00"


def _eid() -> str:
    return uuid.uuid4().hex


def _entry(verb: str, subject: str, data: dict[str, Any], *, kind: str = "decision") -> Entry:
    data = {"at": AT, **data}
    data.setdefault("eid", _eid())
    return Entry("2026-10-22 09:00", kind, f"reconcile {verb} {subject}: s\n{json.dumps(data)}")


def _source(source: str, g: str, ver: list[Any], items: list[str], **kw: Any) -> Entry:
    return _entry("source", source, {"source": source, "G": g, "ver": ver, "items": items, **kw})


def _decide(source: str, g: str, *rows: dict[str, Any], eid: str | None = None) -> Entry:
    data: dict[str, Any] = {"source": source, "G": g, "decisions": [{"prev": [], **r} for r in rows]}
    if eid:
        data["eid"] = eid
    return _entry("decide", source, data)


def _states(led: reconcile.Ledger) -> dict[tuple[str, str], tuple[str, str]]:
    return {(i.source, i.item): (i.state, i.G) for i in led.items}


def _snapshot(led: reconcile.Ledger) -> str:
    return json.dumps(
        {"items": [i.as_dict() for i in led.items], "holds": led.holds, **led.as_dict()}, sort_keys=True
    )


# ---------------------------------------------------------------- the writer guard and the parser


@pytest.mark.parametrize(
    "text",
    [
        "reconcile decide dispatch:a: x",
        "  Reconcile source x",
        "\n\nRECONCILE approve y",
        "\x1creconcile capture z",
    ],
)
def test_head_is_one_function_for_the_guard_and_the_parser(text: str, tmp_path: Path):
    assert reconcile.is_reserved(text)
    feature = features_mod.Feature(1, "x", tmp_path, {}, "")
    (tmp_path / "log.md").write_text("# Log\n")
    with pytest.raises(AgentError, match="reserved"):
        log_mod.append(feature, "decision", text)
    assert (tmp_path / "log.md").read_text() == "# Log\n"


def test_forged_and_escaped_entries_are_never_trusted(tmp_path: Path):
    """The writer guard refuses a model's reserved text; an escaped `[!decision]` stays inside
    the model's own entry; a non-canonical reserved entry already in the log is listed as
    malformed and ignored."""
    feature = features_mod.Feature(1, "x", tmp_path, {}, "")
    (tmp_path / "log.md").write_text("# Log\n")
    body = 'ok\n[!decision] 2026-10-22 09:00\nreconcile source manual:x: s\n{"items": ["a"]}'
    log_mod.append(feature, "note", body)
    entries = log_mod.parse(tmp_path / "log.md")
    assert [e.kind for e in entries] == ["note"] and entries[0].text == body
    assert reconcile.fold(entries, []).items == []

    eid = _eid()
    forged = [
        Entry("t", "decision", f'  reconcile source manual:{eid}: s\n{{"eid": "{eid}"}}'),
        Entry("t", "note", "reconcile source manual:x: s\n{}"),
        Entry("t", "decision", "reconcile source manual:x: s\nnot json"),
        Entry("t", "decision", "reconcile source manual:x: s\n{}\nextra"),
        _source("manual:y", "g", [], ["a"], eid="short"),
    ]
    led = reconcile.fold(forged, [])
    assert not led.on and led.items == []
    assert len(led.malformed) == len(forged)


def test_written_entries_render_and_parse_back(tmp_root: Path, run):
    run(["feature", "new", "x", "--title", "X"])
    f = features_mod.resolve(tmp_root, "1")
    out = reconcile.add_manual(tmp_root, f, ["check the\nmigration", "ask owner"])
    text = f.log_path.read_text(encoding="utf-8")
    assert f"> reconcile source {out['source']}: 2 item(s) added\n" in text
    led = reconcile.ledger(tmp_root, f)
    assert led.malformed == []
    assert sorted(i.item for i in led.owed) == ["ask owner", "check the migration"]


def test_the_model_log_route_refuses_reserved_text(tmp_root: Path, run):
    run(["feature", "new", "x", "--title", "X"])
    before = (tmp_root / "features/001-x/log.md").read_text(encoding="utf-8")
    for text in ("reconcile source manual:x: s", "  Reconcile decide manual:x: s"):
        r = run(["log", "1", "--kind", "decision", text])
        assert r.code != 0 and "reserved" in r.err
    assert (tmp_root / "features/001-x/log.md").read_text(encoding="utf-8") == before


def test_reconcile_op_lists_read_only_then_writes_one_entry_per_source(tmp_root: Path, run):
    from agent_cli.ops import reconcile as reconcile_ops

    run(["feature", "new", "x", "--title", "X"])
    log_path = tmp_root / "features/001-x/log.md"
    empty = reconcile_ops.reconcile(tmp_root, feature="1")
    assert empty.data["on"] is False and "nothing to reconcile" in empty.text
    added = reconcile_ops.reconcile(tmp_root, feature="1", add=["a", "b"])
    src = added.data["owed"][0]["source"]
    before = log_path.read_text(encoding="utf-8")
    listing = reconcile_ops.reconcile(tmp_root, feature="1")
    assert len(listing.data["owed"]) == 2 and log_path.read_text(encoding="utf-8") == before
    done = reconcile_ops.reconcile(
        tmp_root,
        feature="1",
        items=[
            {"source": src, "item": "a", "disposition": "no-task", "reason": "duplicate"},
            {"source": src, "item": "b", "disposition": "waiting", "on": "owner", "until": "owner replies"},
        ],
    )
    assert done.data["owed"] == [] and [w["item"] for w in done.data["waiting"]] == ["b"]
    assert log_path.read_text(encoding="utf-8").count("reconcile decide") == 1
    with pytest.raises(AgentError, match="waiting needs on"):
        reconcile_ops.reconcile(
            tmp_root,
            feature="1",
            items=[{"source": src, "item": "b", "disposition": "waiting", "on": "x", "until": "y"}],
        )


# ---------------------------------------------------------------- the fold


def test_fold_is_identical_over_every_permutation():
    g1, g2 = "1:aaaa", "2:bbbb"
    e1, e2, e3 = _eid(), _eid(), _eid()
    entries = [
        _source("dispatch:001-mac-01", g1, [1], ["result"]),
        _source("dispatch:001-mac-01", g2, [2], ["result", "F1"]),
        _source("dispatch:001-mac-01", g2, [2], ["result", "F1"]),  # a union-merge duplicate
        _decide("dispatch:001-mac-01", g1, {"item": "result", "disposition": "no-task", "reason": "old"}),
        _decide(
            "dispatch:001-mac-01", g2, {"item": "result", "disposition": "no-task", "reason": "a"}, eid=e1
        ),
        _decide(
            "dispatch:001-mac-01", g2, {"item": "result", "disposition": "no-task", "reason": "b"}, eid=e2
        ),
        _decide(
            "dispatch:001-mac-01",
            g2,
            {"item": "F1", "disposition": "waiting", "on": "owner", "until": "reply", "prev": []},
            eid=e3,
        ),
    ]
    snapshots = {_snapshot(reconcile.fold(list(p), [])) for p in itertools.permutations(entries)}
    assert len(snapshots) == 1
    led = reconcile.fold(entries, [])
    assert _states(led) == {
        ("dispatch:001-mac-01", "result"): ("owed", g2),  # two differing heads
        ("dispatch:001-mac-01", "F1"): ("waiting", g2),
    }


def test_new_generation_makes_every_earlier_decision_history():
    src = "dispatch:001-mac-01"
    first = [
        _source(src, "1:a", [1], ["result"]),
        _decide(src, "1:a", {"item": "result", "disposition": "not-accepted", "reason": "x"}),
    ]
    refused = reconcile.fold(first, [])  # not-accepted on a non-task item is malformed
    assert refused.owed and refused.malformed
    first[1] = _decide(src, "1:a", {"item": "result", "disposition": "no-task", "reason": "fine"})
    assert reconcile.fold(first, []).owed == []
    retry = [*first, _source(src, "2:b", [2], ["result"])]
    owed = reconcile.fold(retry, []).owed
    assert [(i.G, i.item) for i in owed] == [("2:b", "result")]
    stale = [*retry, _source(src, "1:a", [1], ["result"])]  # a replayed old source
    assert [i.G for i in reconcile.fold(stale, []).owed] == ["2:b"]


def test_two_generations_at_one_version_fail_closed_then_settle():
    src = "dispatch:001-mac-01"
    entries = [_source(src, "1:a", [1], ["result"]), _source(src, "1:b", [1], ["result", "F1"])]
    led = reconcile.fold(entries, [])
    assert {i.item for i in led.conflicts} == {"result", "F1"}
    pseudo = led.owed[0].G
    assert pseudo.startswith("conflict:") and led.owed[0].conflict == ["1:a", "1:b"]
    assert all(i.G == pseudo for i in led.owed)
    decided = [
        *entries,
        _decide(
            src,
            pseudo,
            {"item": "result", "disposition": "no-task", "reason": "same"},
            {"item": "F1", "disposition": "no-task", "reason": "dup"},
        ),
    ]
    later = reconcile.fold(decided, [])
    assert later.owed == [] and later.conflicts == []
    # deciding the losing raw generation settles nothing
    assert reconcile.fold(
        [*entries, _decide(src, "1:a", {"item": "result", "disposition": "no-task", "reason": "x"})], []
    ).owed


def test_decision_heads():
    src = "manual:" + "a" * 32
    base = _source(src, "a" * 32, [], ["x"])
    a, b = _eid(), _eid()
    one = [base, _decide(src, "a" * 32, {"item": "x", "disposition": "no-task", "reason": "r"}, eid=a)]
    assert reconcile.fold(one, []).owed == []
    equal = [*one, _decide(src, "a" * 32, {"item": "x", "disposition": "no-task", "reason": "r"}, eid=b)]
    assert reconcile.fold(equal, []).owed == []
    differ = [*one, _decide(src, "a" * 32, {"item": "x", "disposition": "no-task", "reason": "other"}, eid=b)]
    owed = reconcile.fold(differ, []).owed
    assert owed and owed[0].heads == sorted([a, b])
    resolved = [
        *differ,
        _decide(src, "a" * 32, {"item": "x", "disposition": "no-task", "reason": "final", "prev": [a, b]}),
    ]
    assert reconcile.fold(resolved, []).owed == []
    # one eid carrying two payloads is malformed, so neither counts
    twin = [base, _decide(src, "a" * 32, {"item": "x", "disposition": "no-task", "reason": "1"}, eid=a)]
    twin.append(_decide(src, "a" * 32, {"item": "x", "disposition": "no-task", "reason": "2"}, eid=a))
    led = reconcile.fold(twin, [])
    assert led.owed and led.malformed[0]["why"] == "one eid with different contents"


def test_task_items_settle_on_tick_and_not_accepted_keeps_the_task_open():
    src = "accept:001-mac-01"
    body = "## Tasks\n\n- [x] done one\n- [ ] still open\n"
    tasks = features_mod.list_tasks(body)
    entries = [_source(src, "1:a@nodiff", [1, ""], ["task#1", "task#2"])]
    assert [i.item for i in reconcile.fold(entries, tasks).owed] == ["task#2"]
    entries.append(
        _decide(src, "1:a@nodiff", {"item": "task#2", "disposition": "not-accepted", "reason": "x"})
    )
    assert reconcile.fold(entries, tasks).owed == []
    assert [t.is_open for t in tasks] == [False, True]


# ---------------------------------------------------------------- activation and self-heal


def _record(root: Path, feature: int, **fields: Any) -> dict[str, Any]:
    d = {
        "id": f"{feature:03d}-mac-01",
        "repo": "harness",
        "short": "mac",
        "feature": feature,
        "kind": "work",
        "attempt": 1,
        "state": "done",
        "tasks": [1],
        "result": {"status": "done", "summary": "s", "findings": [{"severity": "blocking", "summary": "f"}]},
        **fields,
    }
    return state.save(root, d)


def test_old_history_is_never_owed_and_heal_writes_nothing(tmp_root: Path, run):
    run(["feature", "new", "x", "--title", "X"])
    _record(tmp_root, 1, finished="2026-01-01T00:00:00+00:00")
    f = features_mod.resolve(tmp_root, "1")
    before = f.log_path.read_text(encoding="utf-8")
    led = reconcile.ledger(tmp_root, f)
    assert not led.on and led.owed == []
    assert f.log_path.read_text(encoding="utf-8") == before


def test_triggering_result_registers_from_its_intent_not_its_time(tmp_root: Path, run):
    run(["feature", "new", "x", "--title", "X"])
    run(["task", "add", "1", "do it"])
    old = _record(tmp_root, 1, id="001-mac-02", finished="2020-01-01T00:00:00+00:00")
    d = _record(tmp_root, 1, finished="2020-01-01T00:00:00+00:00")
    f = features_mod.resolve(tmp_root, "1")
    d[reconcile.INTENT] = [reconcile.dispatch_source(d), reconcile.accept_source(d, None, feature=f)]
    state.save(tmp_root, d)
    led = reconcile.ledger(tmp_root, f)
    assert sorted((i.source, i.item) for i in led.owed) == [
        ("accept:001-mac-01", "task#1"),
        ("dispatch:001-mac-01", "F1"),
        ("dispatch:001-mac-01", "result"),
    ]
    assert not any(i.source.endswith(old["id"]) for i in led.items)
    assert reconcile.INTENT not in state.load(tmp_root, d["id"])


def test_heal_after_a_crash_appends_once(tmp_root: Path, run):
    run(["feature", "new", "x", "--title", "X"])
    f = features_mod.resolve(tmp_root, "1")
    d = _record(tmp_root, 1)
    pending = reconcile.dispatch_source(d)
    assert pending is not None
    # crash after the append, before the clear
    assert reconcile.append_source(f, pending)
    d[reconcile.INTENT] = [pending]
    state.save(tmp_root, d)
    reconcile.ledger(tmp_root, f)
    reconcile.ledger(tmp_root, f)
    assert f.log_path.read_text(encoding="utf-8").count("reconcile source dispatch:001-mac-01") == 1
    assert reconcile.INTENT not in state.load(tmp_root, d["id"])
    # a duplicate from a concurrent heal or a union merge collapses
    text = f.log_path.read_text(encoding="utf-8")
    entry = text[text.index("> [!decision]") :]
    f.log_path.write_text(text + "\n" + entry, encoding="utf-8")
    assert len(reconcile.ledger(tmp_root, f).owed) == 2


def test_no_result_is_raised_for_questions_probes_or_closed_dispatches():
    base = {"id": "001-mac-01", "feature": 1, "attempt": 1, "result": {}}
    assert reconcile.dispatch_source({**base, "state": "question"}) is None
    assert reconcile.dispatch_source({**base, "state": "cancelled"}) is None
    assert reconcile.dispatch_source({**base, "state": "done", "kind": "probe"}) is None
    assert reconcile.dispatch_source({**base, "state": "failed"}) is not None


def test_landing_journal_intent_registers_once_and_settles_its_obligation(
    tmp_root: Path, run, monkeypatch: pytest.MonkeyPatch
):
    from agent_cli import land

    run(["feature", "new", "x", "--title", "X"])
    monkeypatch.setattr(land, "main_of", lambda root: tmp_root)
    f = features_mod.resolve(tmp_root, "1")
    journal = {
        "id": "20261022T090000-abc123",
        "state": "done",
        "obligations": {"milestone": "recorded", reconcile.OBLIGATION: reconcile.PENDING},
        reconcile.INTENT: [reconcile.landing_source("20261022T090000-abc123", 1)],
    }
    jn.write(tmp_root, journal)
    old = {"id": "20200101T000000-old000", "state": "done", "obligations": {"milestone": "recorded"}}
    jn.write(tmp_root, old)
    led = reconcile.ledger(tmp_root, f)
    assert [(i.source, i.item) for i in led.owed] == [("landing:20261022T090000-abc123", "landing")]
    assert jn.read(tmp_root, journal["id"])["obligations"][reconcile.OBLIGATION] == "done"
    reconcile.ledger(tmp_root, f)
    assert f.log_path.read_text(encoding="utf-8").count("reconcile source landing:") == 1


# ---------------------------------------------------------------- capture and approval


def _manual(root: Path) -> tuple[features_mod.Feature, str]:
    f = features_mod.resolve(root, "1")
    return f, reconcile.add_manual(root, f, ["finding"])["source"]


def test_capture_after_a_crash_creates_exactly_one_task(tmp_root: Path, run, monkeypatch: pytest.MonkeyPatch):
    run(["feature", "new", "x", "--title", "X"])
    f, src = _manual(tmp_root)
    original = features_mod.add_task_at

    def crash(body: str, text: str) -> tuple[str, int]:
        raise OSError("killed before the task was saved")

    monkeypatch.setattr(features_mod, "add_task_at", crash)
    with pytest.raises(OSError):
        reconcile.capture(tmp_root, f, source=src, item="finding", text="fix it")
    assert "reconcile capture" in f.log_path.read_text(encoding="utf-8")
    assert features_mod.list_tasks(features_mod.resolve(tmp_root, "1").body) == []
    monkeypatch.setattr(features_mod, "add_task_at", original)

    real_write = reconcile._write

    def crash_decide(feature: Any, verb: str, *a: Any) -> dict[str, Any]:
        if verb == "decide":
            raise OSError("killed after the task was added")
        return real_write(feature, verb, *a)

    monkeypatch.setattr(reconcile, "_write", crash_decide)
    with pytest.raises(OSError):
        reconcile.capture(tmp_root, f, source=src, item="finding", text="fix it")
    assert len(features_mod.list_tasks(features_mod.resolve(tmp_root, "1").body)) == 1
    monkeypatch.setattr(reconcile, "_write", real_write)

    out = reconcile.capture(tmp_root, f, source=src, item="finding", text="fix it")
    tasks = features_mod.list_tasks(features_mod.resolve(tmp_root, "1").body)
    assert len(tasks) == 1 and reconcile.cap_marker(out["capture"]) in tasks[0].text
    assert f.log_path.read_text(encoding="utf-8").count("reconcile capture") == 1
    again = reconcile.capture(tmp_root, f, source=src, item="finding", text="fix it")
    assert again["changed"] is False
    led = reconcile.ledger(tmp_root, f)
    assert led.owed == [] and led.holds == {1: out["capture"][:12]}
    assert features_mod.resolve(tmp_root, "1").status == "active"

    approved = reconcile.approve(tmp_root, f, of=out["capture"][:12], evidence="owner said yes")
    assert approved["changed"] and reconcile.ledger(tmp_root, f).holds == {}
    assert reconcile.approve(tmp_root, f, of=out["capture"], evidence="again")["changed"] is False


def test_two_captures_claiming_one_task_fail_closed():
    src_a, src_b = "manual:" + "a" * 32, "manual:" + "b" * 32
    cap_a, cap_b = _eid(), _eid()
    body = f"## Tasks\n\n- [ ] one (cap {cap_a[:12]})\n"
    tasks = features_mod.list_tasks(body)
    entries = [
        _source(src_a, "a" * 32, [], ["x"]),
        _source(src_b, "b" * 32, [], ["y"]),
        _entry("capture", src_a, {"eid": cap_a, "source": src_a, "G": "a" * 32, "item": "x", "text": "one"}),
        _entry("capture", src_b, {"eid": cap_b, "source": src_b, "G": "b" * 32, "item": "y", "text": "two"}),
        _decide(src_a, "a" * 32, {"item": "x", "disposition": "new-task", "capture": cap_a, "task": 1}),
    ]
    assert [i.item for i in reconcile.fold(entries, tasks).owed] == ["y"]
    entries.append(
        _decide(src_b, "b" * 32, {"item": "y", "disposition": "new-task", "capture": cap_b, "task": 1})
    )
    owed = reconcile.fold(entries, tasks).owed
    assert {i.item for i in owed} == {"x", "y"}
    assert all("claim task #1" in i.reason for i in owed)
