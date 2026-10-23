"""Preview-time notes files and the exact composed bytes sent at launch."""

import shutil
import threading
from pathlib import Path

import pytest

from agent_cli import core, dispatch, leases, links, notes, notes_writer, worktree
from agent_cli.core import AgentError
from agent_cli.dispatch import allocation, label_claims, lifecycle, orders
from agent_cli.ops.dispatch import dispatch_preview_text
from dispatch_helpers import PROMPT, _new, _new_fast, envelope, one, settle
from dispatch_helpers import droot as droot


@pytest.fixture(autouse=True)
def orders_notes(droot: Path):
    notes_root = notes.read_notes_root(droot)
    assert notes_root is not None
    shutil.move(str(droot / "features"), str(notes_root / "features"))
    links.make(droot / "features", notes_root / "features", is_dir=True)


@pytest.mark.parametrize("windows_newlines", [False, True])
def test_preview_file_in_notes_equals_launched_bytes(droot: Path, fake_claude, monkeypatch, windows_newlines):
    if windows_newlines:
        original = core.os.fdopen

        def fdopen(fd, mode, **kwargs):
            if "b" not in mode:
                kwargs["newline"] = kwargs.get("newline") or "\r\n"
            return original(fd, mode, **kwargs)

        monkeypatch.setattr(core.os, "fdopen", fdopen)
    preview = _new_fast(droot, dry_run=True)
    order = droot / preview["work_order"]
    notes_root = notes.read_notes_root(droot)
    assert notes_root is not None and order.resolve().is_relative_to(notes_root.resolve())
    approved = order.read_bytes()
    assert approved == preview["prompt"].encode("utf-8")
    shown = dispatch_preview_text(preview)
    assert shown.index(f"`{preview['work_order_url']}`") < shown.index("Launch this order?")
    assert f"`{preview['work_order_url']}`" in shown.split("Launch this order?", 1)[1]
    assert not dispatch.load_all(droot)
    fake_claude.scenario([one(envelope())])
    launched = _new_fast(droot, preview_id=preview["preview_id"])
    assert launched["work_order_url"] == preview["work_order_url"]
    assert order.read_bytes() == approved
    assert dispatch.file(droot, launched["id"], ".prompt.md").read_bytes() == approved
    assert fake_claude.calls()[0]["prompt"].encode("utf-8") == approved
    if not windows_newlines:  # the preview launch's worktree stays reusable once it completes
        settle(droot, launched)
        second = _new_fast(droot)
        assert second["reused"] and second["worktree"] == launched["worktree"]


@pytest.mark.parametrize("change", ["file", "prompt", "budget", "context", "manifest"])
def test_changed_preview_cannot_launch(droot: Path, fake_claude, change: str):
    preview = _new_fast(droot, dry_run=True)
    kw = {}
    if change == "file":
        (droot / preview["work_order"]).write_text("changed ordinary order\n", encoding="utf-8")
    elif change == "prompt":
        kw["prompt"] = PROMPT + "\nAdd another ordinary line."
    elif change == "budget":
        kw.update(budget=2.0, budget_reason="more ordinary work")
    elif change == "manifest":
        orders._path(droot, preview["preview_id"]).write_text("[]\n", encoding="utf-8")
    else:
        plan = droot / "features/001-alpha-work/plan.md"
        plan.write_text(plan.read_text().replace("the alpha service", "the beta service"), encoding="utf-8")
    with pytest.raises(AgentError, match="preview again"):
        _new_fast(droot, preview_id=preview["preview_id"], **kw)
    assert not dispatch.load_all(droot)
    assert not fake_claude.calls()
    updated = _new_fast(droot, dry_run=True, **kw)
    assert updated["preview_id"] != preview["preview_id"]
    if change == "budget":  # one relaunch shows a re-preview carries the changed arguments
        fake_claude.scenario([one(envelope())])
        assert _new_fast(droot, preview_id=updated["preview_id"], **kw)["state"] == "done"


def _files(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("unavailable", ["no-notes", "linked-worktree", "not-writer", "write-error"])
def test_unavailable_order_still_previews_and_launches_with_verification(
    droot: Path, fake_claude, monkeypatch, unavailable: str
):
    if unavailable == "no-notes":
        # Keep the independent label-ownership prerequisite satisfied before losing notes.
        label_claims.require(droot, "mac")
        (droot / notes.NOTES_ROOT_FILE).unlink()
    elif unavailable == "linked-worktree":
        monkeypatch.setattr(worktree, "is_worktree", lambda root: True)
    elif unavailable == "not-writer":
        monkeypatch.setattr(notes_writer, "writes_shared_notes", lambda root: False)
    else:

        def refused(*args):
            raise PermissionError("order directory is not writable")

        monkeypatch.setattr(notes_writer, "write_work_order", refused)
    before = _files(droot)
    preview = _new(droot, dry_run=True)
    assert preview["work_order_url"] is None
    assert preview["work_order_reason"] in dispatch_preview_text(preview)
    assert preview["prompt"].startswith("# Work order")
    assert _files(droot) == before
    fake_claude.scenario([one(envelope(), commit="add the ordinary line")])
    # One reason runs the verifier to show a launch without an order is still verified; the
    # verifier never reads the order, so the other reasons skip it.
    verified = unavailable == "no-notes"
    launched = _new(droot) if verified else _new_fast(droot)
    assert launched["state"] == "done"
    if verified:
        assert launched["verification_result"]["status"] == "verified"
    assert "work_order_url" not in launched
    prompt = dispatch.file(droot, launched["id"], ".prompt.md").read_text(encoding="utf-8")
    assert fake_claude.calls()[0]["prompt"] == prompt


def test_permission_preflight_refusal_writes_nothing(droot: Path, fake_claude):
    before = _files(droot)
    with pytest.raises(AgentError, match="preflight refused this launch"):
        _new(droot, prompt=PROMPT + "\n```sh\npnpm --filter x lint\n```\n")
    assert _files(droot) == before
    assert not fake_claude.calls()


def test_an_unavailable_repreview_keeps_the_previous_order(droot: Path, fake_claude, monkeypatch):
    first = _new_fast(droot, dry_run=True)
    approved = (droot / first["work_order"]).read_bytes()
    monkeypatch.setattr(notes_writer, "writes_shared_notes", lambda root: False)
    prompt = PROMPT + "\nDo the revised ordinary work."
    preview = _new_fast(droot, prompt=prompt, dry_run=True)
    assert preview["work_order_url"] is None
    assert not orders._path(droot, preview["preview_id"]).exists()
    assert (droot / first["work_order"]).read_bytes() == approved
    # Plan §A3 (plan.md:64) retires replacing an earlier order with the next preview.
    assert orders.preview(droot, first["preview_id"])
    fake_claude.scenario([one(envelope())])
    assert _new_fast(droot, preview_id=first["preview_id"])["state"] == "done"


def test_only_the_allocation_winner_publishes_its_order(droot: Path, monkeypatch):
    barrier = threading.Barrier(2)
    allocate = allocation.allocate
    write = notes_writer.write_work_order
    published = []
    results = []

    checked = threading.Barrier(2)
    check = orders.check

    def same_id(*args):
        picked = allocate(*args)
        barrier.wait(5)
        return picked

    def both_checked(*args):
        # Both launches pass the order check before either claims: the race is in the claim.
        check(*args)
        checked.wait(5)

    def after_claim(root, relative, text):
        dispatch_id = Path(relative).stem
        assert dispatch.file(root, dispatch_id, ".json").exists()
        published.append(dispatch_id)
        return write(root, relative, text)

    def launch(index):
        try:
            results.append(_new_fast(droot, prompt=PROMPT + f"\nOrdinary job {index}.", watch=False))
        except Exception as exc:  # Keep both thread outcomes visible to the assertion.
            results.append(exc)

    monkeypatch.setattr(allocation, "allocate", same_id)
    monkeypatch.setattr(orders, "check", both_checked)
    monkeypatch.setattr(notes_writer, "write_work_order", after_claim)
    monkeypatch.setattr(lifecycle, "start", lambda root, d, **kw: d)
    threads = [threading.Thread(target=launch, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    winners = [result for result in results if isinstance(result, dict)]
    refusals = [result for result in results if isinstance(result, AgentError)]
    assert len(results) == 2 and len(winners) == len(refusals) == 1
    assert "concurrent `dispatch new`" in str(refusals[0])
    winner = winners[0]
    assert published == [winner["id"]]
    order = droot / winner["work_order"]
    assert list(order.parent.glob("*.md")) == [order]
    assert order.read_bytes() == dispatch.file(droot, winner["id"], ".prompt.md").read_bytes()
    assert orders.preview(droot, winner["id"])["nonce"] == winner["nonce"]
    assert orders.preview(droot, winner["id"])["sha256"] == winner["work_order_sha256"]


def test_preview_changed_during_claim_refuses_and_releases_allocation(droot: Path, fake_claude, monkeypatch):
    preview = _new_fast(droot, dry_run=True)
    order = droot / preview["work_order"]
    claim = allocation.claim

    def changed_after_check(*args, **kwargs):
        claim(*args, **kwargs)
        order.write_text("changed ordinary order\n", encoding="utf-8")

    monkeypatch.setattr(allocation, "claim", changed_after_check)
    with pytest.raises(AgentError, match="work-order file changed"):
        _new_fast(droot)
    assert not dispatch.load_all(droot)
    assert not leases.list_all(droot)
    assert not fake_claude.calls()
    assert order.read_text() == "changed ordinary order\n"


@pytest.mark.parametrize(("reverse", "idle_worktree"), [(False, True), (True, False)])
def test_three_order_wave_launches_without_repreview(
    droot: Path, monkeypatch, fake_claude, idle_worktree, reverse
):
    previous = None
    if idle_worktree:
        fake_claude.scenario([one(envelope())])
        previous = _new_fast(droot)
        settle(droot, previous)
    monkeypatch.setattr(lifecycle, "start", lambda root, d, **kw: d)
    jobs = [{"prompt": PROMPT + f"\nOrdinary job {i}.", "writes": [f"job-{i}.txt"]} for i in range(3)]
    prepared = [_new_fast(droot, dry_run=True, **job) for job in jobs]
    assert len({p["preview_id"] for p in prepared}) == 3
    assert len({p["work_order_url"] for p in prepared}) == 3
    approved = [(droot / p["work_order"]).read_bytes() for p in prepared]
    assert len(dispatch.load_all(droot)) == int(idle_worktree)
    assert not leases.list_all(droot)
    wave = list(zip(prepared, jobs, strict=True))
    launched = [
        _new_fast(droot, preview_id=p["preview_id"], **job)
        for p, job in (reversed(wave) if reverse else wave)
    ]
    assert len({d["id"] for d in launched}) == 3
    assert len({d["worktree"] for d in launched}) == 3
    by_preview = {d["preview_id"]: d for d in launched}
    if previous is not None:
        reused = by_preview[prepared[0]["preview_id"]]
        assert reused["worktree"] == previous["worktree"] and reused["reused"]
    for p, text in zip(prepared, approved, strict=True):
        d = by_preview[p["preview_id"]]
        assert d["work_order_url"] == p["work_order_url"]
        assert (droot / d["work_order"]).read_bytes() == text
        assert dispatch.file(droot, d["id"], ".prompt.md").read_bytes() == text
    with pytest.raises(AgentError, match="already launched"):
        _new_fast(droot, preview_id=prepared[0]["preview_id"], **jobs[0])


def test_missing_preview_id_cannot_launch(droot: Path, fake_claude):
    with pytest.raises(AgentError, match="preview missing"):
        _new_fast(droot, preview_id="preview-unknown")
    assert not dispatch.load_all(droot)
    assert not fake_claude.calls()
