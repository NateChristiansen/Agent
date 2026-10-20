"""The document command's scope, byte preservation and cooperating-writer contract."""

from __future__ import annotations

import hashlib
import multiprocessing
import os
from pathlib import Path

import pytest

from agent_cli import documents
from agent_cli.core import AgentError

PLAN = b"""---
id: 1
slug: sample
title: Sample
status: active
---
# Sample

## Context
Old context.

## Tasks
- [ ] Keep this task

## Verification
Check it.

## Handoffs
<!-- populated by `agent dispatch` -->

## Notes
"""
TARGET = "features/001-sample/plan.md"
# The refusal that replaces these on Windows runs everywhere, in test_documents_refusal.py.
pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="notes_document is unsupported on Windows: it needs POSIX directory locks and no-follow access",
)


def _vault(tmp_path: Path, linked: bool) -> Path:
    root = tmp_path / "harness"
    (root / "bin").mkdir(parents=True)
    (root / "bin/agent").touch()
    if linked:
        private = tmp_path / "private"
        (private / "features").mkdir(parents=True)
        (root / "features").symlink_to("../private/features", target_is_directory=True)
        (root / ".agent").mkdir()
        (root / ".agent/notes-root").write_text("../private\n")
    else:
        (root / "features").mkdir()
    feature = root / "features/001-sample"
    feature.mkdir()
    (feature / "plan.md").write_bytes(PLAN)
    (feature / "log.md").write_text("CLI-owned log\n")
    return root


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """A plain vault: `features/` is a real directory.

    This used to be `params=[False, True]`, which ran every test in the file twice and doubled
    46 tests to 92 for a `notes-root` variant that changes nothing in most of the code paths
    under test (2026-09-24). The variant is security-relevant — it is the symlink redirection
    that keeps private notes out of the public repo — so the tests that actually exercise it
    take `linked_vault` below, by name, rather than getting it by accident."""
    return _vault(tmp_path, linked=False)


@pytest.fixture
def linked_vault(tmp_path: Path) -> Path:
    """A vault whose `features/` is a symlink into a private notes repo, with `.agent/notes-root`
    naming it. The scope, refusal and race tests that care about redirection use this."""
    return _vault(tmp_path, linked=True)


def draft(
    tmp_path: Path, text: bytes = PLAN.replace(b"Old context.", b"New $value `literal`\nNext line.")
) -> Path:
    source = tmp_path / "replacement.md"
    source.write_bytes(text)
    return source


def command(root: Path, source: Path, target: str = TARGET) -> list[str]:
    return ["--root", str(root), "--json", "notes", "document", target, "--from-file", str(source)]


@pytest.mark.parametrize("linked", [False, True], ids=["plain", "linked"])
def test_cli_preview_apply_preserves_bytes(tmp_path: Path, run, linked: bool):
    """The one positive apply that still runs against both vault shapes. Everything else in this
    file uses the plain one; redirection is covered by the refusal and race tests below."""
    vault = _vault(tmp_path, linked=linked)
    source = draft(tmp_path)
    args = command(vault, source)
    preview = run(args)
    assert preview.code == 0, preview.err
    result = preview.json
    assert result["target"] == TARGET
    assert result["resolved_target"] == str((vault / TARGET).resolve())
    assert result["before_sha256"] == hashlib.sha256(PLAN).hexdigest()
    assert "-Old context." in result["diff"]
    assert "+New $value `literal`" in result["diff"]
    assert not result["applied"]
    assert (vault / TARGET).read_bytes() == PLAN
    applied = run([*args, "--apply", "--expect-sha256", result["before_sha256"]])
    assert applied.code == 0, applied.err
    assert applied.json["applied"]
    assert (vault / TARGET).read_bytes() == source.read_bytes()
    assert (vault / "features/001-sample/log.md").read_text() == "CLI-owned log\n"
    assert not list((vault / TARGET).parent.glob(".document-*.tmp"))


def test_text_preview_and_create(vault: Path, tmp_path: Path, run):
    source = draft(tmp_path, b"# Audit\n\n$literal `command`\nsecond line\n")
    target = "features/001-sample/audit.md"
    args = command(vault, source, target)
    assert run(args).code != 0
    assert run([*args, "--create"]).code != 0
    created_args = [*args, "--create", "--expect-absent"]
    preview = run([arg for arg in created_args if arg != "--json"])
    assert preview.code == 0, preview.err
    assert "preview only" in preview.out and "--- /dev/null" in preview.out
    assert f"resolved target: {(vault / target).resolve()}" in preview.out
    assert not (vault / target).exists()
    assert run([*created_args, "--apply"]).code == 0
    assert (vault / target).read_bytes() == source.read_bytes()
    assert run([*created_args, "--apply"]).code != 0
    source.write_text("whole replacement\n")
    preview = run(args).json
    assert run([*args, "--apply", "--expect-sha256", preview["before_sha256"]]).code == 0
    assert (vault / target).read_bytes() == b"whole replacement\n"


def test_apply_needs_digest_and_rejects_stale_task_update(vault: Path, tmp_path: Path, run):
    args = command(vault, draft(tmp_path))
    assert run([*args, "--apply"]).code != 0
    preview = run(args).json
    updated = PLAN.replace(b"[ ]", b"[x]")
    (vault / TARGET).write_bytes(updated)
    result = run([*args, "--apply", "--expect-sha256", preview["before_sha256"]])
    assert result.code != 0 and "stale SHA-256" in result.err
    assert (vault / TARGET).read_bytes() == updated


@pytest.mark.parametrize(
    "before,after",
    [
        (b"status: active", b"status: done"),
        (b"title: Sample", b"title: Other"),
        (b"[ ] Keep", b"[x] Keep"),
        (b"populated by", b"changed by"),
        (b"## Tasks", b"## tasks"),
        (b"## Tasks", b"## Tasks extra"),
        (b"## Tasks", b" ## Tasks"),
        (b"## Tasks", b"## Tasks ##"),
        (b"## Tasks", b"```\n## Tasks"),
        (b"## Tasks", b"<!--\n## Tasks"),
        (b"## Context", b"## Tasks\n- [ ] Hidden duplicate\n\n## Context"),
        (b"## Context", b"```md\n## Tasks\n- [ ] Fake\n```\n## Context"),
    ],
)
def test_plan_rejects_owned_or_ambiguous_changes(vault: Path, tmp_path: Path, run, before, after):
    result = run(command(vault, draft(tmp_path, PLAN.replace(before, after))))
    assert result.code != 0
    assert (vault / TARGET).read_bytes() == PLAN


def test_crlf_frontmatter_stays_byte_exact(vault: Path, tmp_path: Path, run):
    original = PLAN.replace(b"\n", b"\r\n")
    (vault / TARGET).write_bytes(original)
    args = command(vault, draft(tmp_path, original.replace(b"Old context.", b"New.")))
    preview = run(args)
    assert preview.code == 0, preview.err
    assert run([*args, "--apply", "--expect-sha256", preview.json["before_sha256"]]).code == 0
    assert (vault / TARGET).read_bytes() == original.replace(b"Old context.", b"New.")


@pytest.mark.parametrize(
    "target",
    [
        "notes/audit.md",
        "repos/x/design.md",
        "STATUS.md",
        "agent.yaml",
        "features/001-sample/../audit.md",
        "features/001-sample/.hidden.md",
        "features/001-sample/handoffs/audit.md",
        "features/001-sample/log.md",
        "features/001-sample/LOG.md",
        "features/001-sample/PLAN.md",
        "features/001-sample/status.md",
        "features/001-sample/index.md",
        "features/001-sample/audit.md/",
        "features/001-sample/log.md.",
        "/features/001-sample/audit.md",
        "features//001-sample/audit.md",
        "features/001-sample/audit.txt",
        "features/999-absent/audit.md",
    ],
)
def test_refuses_out_of_scope_paths(vault: Path, tmp_path: Path, run, target: str):
    result = run([*command(vault, draft(tmp_path), target), "--create", "--expect-absent", "--apply"])
    assert result.code != 0
    assert (vault / TARGET).read_bytes() == PLAN


@pytest.mark.parametrize("kind", ["document", "feature", "plan", "hardlink"])
def test_refuses_symlinks_and_hardlinks(linked_vault: Path, tmp_path: Path, run, kind: str):
    vault = linked_vault  # the harder shape: a redirected features/ plus the planted link
    outside = tmp_path / "outside"
    outside.mkdir()
    foreign = outside / "audit.md"
    foreign.write_text("unchanged\n")
    target = "features/001-sample/audit.md"
    if kind == "feature":
        target = "features/002-escape/audit.md"
        (vault / "features/002-escape").symlink_to(outside, target_is_directory=True)
    elif kind == "plan":
        (vault / TARGET).unlink()
        (vault / TARGET).symlink_to(foreign)
    elif kind == "hardlink":
        os.link(foreign, vault / target)
    else:
        (vault / target).symlink_to(foreign)
    result = run(command(vault, draft(tmp_path), target))
    assert result.code != 0
    assert foreign.read_text() == "unchanged\n"


def test_unregistered_or_redirected_feature_root(linked_vault: Path, tmp_path: Path, run):
    """Only meaningful against a redirected `features/`, so it takes `linked_vault` directly.
    It used to run twice and return immediately on the plain half."""
    vault = linked_vault
    marker = vault / ".agent/notes-root"
    marker.unlink()
    args = command(vault, draft(tmp_path))
    assert "registered notes root" in run(args).err
    marker.write_text("../somewhere-else\n")
    assert "registered feature root" in run(args).err
    marker.write_text("../private\n")
    actual = tmp_path / "private/features"
    actual.rename(tmp_path / "moved-features")
    actual.symlink_to("../moved-features", target_is_directory=True)
    assert run(args).code != 0


def test_existing_feature_identity_required(vault: Path, tmp_path: Path, run):
    (vault / TARGET).write_bytes(PLAN.replace(b"id: 1", b"id: 999"))
    result = run(
        [*command(vault, draft(tmp_path), "features/001-sample/audit.md"), "--create", "--expect-absent"]
    )
    assert result.code != 0 and "identity/status" in result.err


def _competing_edit(root, source, digest, ready, go, results):
    ready.put(True)
    go.wait(10)
    try:
        documents.edit(Path(root), target=TARGET, source=Path(source), apply=True, expect_sha256=digest)
        results.put("applied")
    except AgentError as exc:
        results.put(exc.message)


def test_competing_document_processes_cannot_both_accept_same_digest(linked_vault: Path, tmp_path: Path):
    vault = linked_vault  # races are the case where redirection matters most
    sources = [tmp_path / "first.md", tmp_path / "second.md"]
    for index, source in enumerate(sources):
        source.write_bytes(PLAN.replace(b"Old context.", f"Writer {index}".encode()))
    ctx = multiprocessing.get_context("spawn")
    ready, results, go = ctx.Queue(), ctx.Queue(), ctx.Event()
    processes = [
        ctx.Process(
            target=_competing_edit,
            args=(str(vault), str(source), hashlib.sha256(PLAN).hexdigest(), ready, go, results),
        )
        for source in sources
    ]
    try:
        for process in processes:
            process.start()
        for _ in processes:
            assert ready.get(timeout=10)
        go.set()
        outcomes = [results.get(timeout=10) for _ in processes]
        assert outcomes.count("applied") == 1
        assert sum("stale SHA-256" in result for result in outcomes) == 1
        assert (vault / TARGET).read_bytes() in [source.read_bytes() for source in sources]
    finally:
        go.set()
        for process in processes:
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)


def test_recheck_catches_noncooperating_writer_during_preparation(vault: Path, tmp_path: Path, monkeypatch):
    source = draft(tmp_path)
    actual_fsync = documents.os.fsync

    def changed_during_flush(fd):
        actual_fsync(fd)
        (vault / TARGET).write_bytes(PLAN.replace(b"[ ]", b"[x]"))

    monkeypatch.setattr(documents.os, "fsync", changed_during_flush)
    with pytest.raises(AgentError, match="changed during preparation"):
        documents.edit(
            vault, target=TARGET, source=source, apply=True, expect_sha256=hashlib.sha256(PLAN).hexdigest()
        )
    assert b"[x]" in (vault / TARGET).read_bytes()
    assert not list((vault / TARGET).parent.glob(".document-*.tmp"))


def test_symlink_swap_during_preparation_never_writes_through(
    linked_vault: Path, tmp_path: Path, monkeypatch
):
    vault = linked_vault
    source = draft(tmp_path)
    victim = tmp_path / "victim.md"
    victim.write_text("untouched\n")
    actual_fsync = documents.os.fsync

    def swap_during_flush(fd):
        actual_fsync(fd)
        (vault / TARGET).unlink()
        (vault / TARGET).symlink_to(victim)

    monkeypatch.setattr(documents.os, "fsync", swap_during_flush)
    with pytest.raises(AgentError, match="access refused"):
        documents.edit(
            vault, target=TARGET, source=source, apply=True, expect_sha256=hashlib.sha256(PLAN).hexdigest()
        )
    assert victim.read_text() == "untouched\n"
    assert not list((vault / TARGET).parent.glob(".document-*.tmp"))


def test_creation_race_never_overwrites_appearing_file(vault: Path, tmp_path: Path, monkeypatch):
    target = "features/001-sample/audit.md"
    source = draft(tmp_path, b"my audit\n")
    actual_link = documents.os.link

    def competing_link(*args, **kwargs):
        (vault / target).write_text("other writer\n")
        return actual_link(*args, **kwargs)

    monkeypatch.setattr(documents.os, "link", competing_link)
    with pytest.raises(AgentError, match="access refused"):
        documents.edit(vault, target=target, source=source, apply=True, create=True, expect_absent=True)
    assert (vault / target).read_text() == "other writer\n"
    assert not list((vault / target).parent.glob(".document-*.tmp"))


def test_diff_marks_missing_final_newline(vault: Path, tmp_path: Path, run):
    target = "features/001-sample/audit.md"
    (vault / target).write_bytes(b"old")
    result = run(command(vault, draft(tmp_path, b"new"), target))
    assert result.code == 0, result.err
    assert "-old\n\\ No newline at end of file\n+new\n" in result.json["diff"]
