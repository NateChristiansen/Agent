"""each verifier run's `PYTEST_DEBUG_TEMPROOT` is a fresh `mkdtemp` directory under
the system temp directory, outside every git checkout, recorded in its run area
`.agent/dispatches/<id>.verification/<run_id>/`. It is made before the command and removed after
it without touching the evidence. `TMPDIR` is left alone."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from agent_cli import repos
from agent_cli.dispatch import lane, verification
from agent_cli.dispatch import reverify as reverify_mod
from agent_cli.dispatch import verification_gate as gate
from agent_cli.dispatch.options import verifier_env
from agent_cli.dispatch.state import load
from agent_cli.dispatch.verification_evidence import Binding
from agent_cli.launcher import is_windows
from dispatch_helpers import _set_alpha_dispatch, droot  # noqa: F401
from test_dispatch_reverify import _blocked_by_dirt
from test_dispatch_verification import ready  # noqa: F401


def _prepare(root: Path, d: dict):
    repo = repos.load_registry(root).require("alpha")
    head = gate.worktree_head(Path(d["worktree"]))
    assert repo.verification is not None
    return lane.prepare(
        root,
        Path(d["worktree"]),
        dispatch_id=d["id"],
        attempt=d["attempt"],
        final_sha=head,
        config=repo.verification,
        environment=verifier_env(repo),
    )


def _end(root: Path, prepared) -> None:
    verification.request_cancel(root, prepared.binding)
    verification.recover(root, prepared.binding)
    lane.clear(root, prepared.binding)


def _recorded(root: Path, row: dict) -> Path:
    return Path((root / row["run_area"] / "pytest-temproot").read_text().strip())


def test_the_temp_root_is_outside_any_git_checkout_and_the_harness_root(ready):
    root, d = ready
    prepared = _prepare(root, d)
    temp = Path(prepared.environment["PYTEST_DEBUG_TEMPROOT"])
    assert temp.is_dir()
    # Audit 10 plan.md:46 (§A1): Windows retires the long profile temp root, keeping isolation.
    expected = Path(d["worktree"]).anchor + "Temp" if is_windows() else tempfile.gettempdir()
    assert Path(expected).resolve() in temp.parents
    assert temp.name.startswith(lane._prefix(prepared.binding))
    inside = subprocess.run(
        ["git", "-C", str(temp), "rev-parse", "--is-inside-work-tree"], capture_output=True, check=False
    )
    assert inside.returncode != 0
    assert root.resolve() not in temp.parents
    assert Path(d["worktree"]).resolve() not in temp.parents
    _end(root, prepared)


def test_windows_temp_parent_is_short_and_posix_placement_is_unchanged(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from typing import cast

    monkeypatch.setattr(lane, "is_windows", lambda: True)
    parent = lane.temp_parent(cast(Path, SimpleNamespace(anchor=str(tmp_path))))
    assert parent is not None
    assert parent == tmp_path / "Temp" and parent.is_dir()
    monkeypatch.setattr(lane, "is_windows", lambda: False)
    assert lane.temp_parent(tmp_path) is None


def test_the_temp_root_name_is_short_and_names_only_its_own_run(ready):
    # The old name carried both full ids (about 100 characters), and git's object paths under a
    # Windows verifier's pytest dirs then passed the path limit.
    root, d = ready
    first, second = _prepare(root, d), _prepare(root, d)
    names = [Path(p.environment["PYTEST_DEBUG_TEMPROOT"]).name for p in (first, second)]
    assert all(len(name) <= 28 for name in names)
    assert lane._prefix(first.binding) != lane._prefix(second.binding)
    assert not names[1].startswith(lane._prefix(first.binding))
    assert not names[0].startswith(lane._prefix(second.binding))
    for prepared in (first, second):
        _end(root, prepared)


@pytest.mark.parametrize("error", [PermissionError("locked-down root"), OSError("UNC share unavailable")])
def test_prepare_falls_back_to_system_temp_when_windows_temp_parent_cannot_be_created(
    ready, monkeypatch, error
):
    root, d = ready
    parent = Path(Path(d["worktree"]).anchor) / "Temp"
    mkdir = Path.mkdir

    def refused(path, *args, **kwargs):
        if path == parent:
            raise error
        return mkdir(path, *args, **kwargs)

    monkeypatch.setattr(lane, "is_windows", lambda: True)
    monkeypatch.setattr(Path, "mkdir", refused)
    prepared = _prepare(root, d)
    temp = Path(prepared.environment["PYTEST_DEBUG_TEMPROOT"])
    try:
        assert temp.is_dir() and temp.parent == Path(tempfile.gettempdir()).resolve()
        assert lane._temp(root, prepared.binding) == temp
    finally:
        _end(root, prepared)
    assert not temp.exists()


def test_a_record_naming_another_runs_temp_root_is_refused_and_that_root_kept(ready):
    root, d = ready
    mine, theirs = _prepare(root, d), _prepare(root, d)
    temps = [Path(p.environment["PYTEST_DEBUG_TEMPROOT"]) for p in (mine, theirs)]
    record = root / lane.run_area(mine.binding) / "pytest-temproot"
    record.write_text(f"{temps[1]}\n")
    with pytest.raises(ValueError, match="does not name this run's temp root"):
        lane._temp(root, mine.binding)
    assert not lane.clear(root, mine.binding) and temps[1].is_dir()
    assert lane.run_busy(root, mine.binding)
    record.write_text(f"{temps[0]}\n")
    for prepared in (mine, theirs):
        _end(root, prepared)
    assert not any(t.exists() for t in temps)


def test_two_runs_get_their_own_temp_roots_and_cleaning_one_keeps_the_other(ready):
    root, d = ready
    first, second = _prepare(root, d), _prepare(root, d)
    temps = [Path(p.environment["PYTEST_DEBUG_TEMPROOT"]) for p in (first, second)]
    assert temps[0] != temps[1]
    for prepared, temp in zip((first, second), temps, strict=True):
        record = root / lane.run_area(prepared.binding) / "pytest-temproot"
        assert record.read_text().strip() == str(temp) and temp.is_dir()
        (temp / "basetemp.txt").write_text("pytest scratch")
    assert lane.clear(root, first.binding)
    assert not temps[0].exists() and (temps[1] / "basetemp.txt").exists()
    assert (root / lane.run_area(first.binding) / "lifecycle.json").exists()
    assert (root / lane.run_area(first.binding) / "initial.json").exists()
    assert (root / lane.run_area(first.binding) / "pytest-temproot").exists()
    for prepared in (first, second):
        _end(root, prepared)
    assert not temps[1].exists()


def test_read_only_files_in_the_temp_root_do_not_keep_the_lane(ready, tmp_path):
    # Git writes its objects read-only; on Windows that kept 043-windows-06's temp root and lane.
    import stat

    root, d = ready
    prepared = _prepare(root, d)
    temp = Path(prepared.environment["PYTEST_DEBUG_TEMPROOT"])
    objects = temp / "pytest-of-x" / "repository" / ".git" / "objects" / "1c"
    objects.mkdir(parents=True)
    blob = objects / "b7b8e2"
    blob.write_text("object")
    blob.chmod(stat.S_IREAD)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    outside.chmod(stat.S_IREAD)
    try:
        (objects / "link").symlink_to(outside)
    except OSError:
        pass  # Windows without the symlink privilege
    _end(root, prepared)
    assert not temp.exists()
    assert not lane.run_busy(root, prepared.binding)
    assert outside.read_text() == "keep" and not outside.stat().st_mode & stat.S_IWRITE


def test_install_replaces_entries_whatever_their_kind_or_mode(tmp_path):
    # Review of 043-lane-readonly-rmtree (043-windows-10): a directory replaced by a file kept the
    # directory, and a read-only file raised.
    import stat

    import seeded

    template, dest = tmp_path / "template", tmp_path / "dest"
    for where in (template, dest):
        where.mkdir()
    (template / "was_dir").write_text("file now")
    (dest / "was_dir").mkdir()
    (dest / "was_dir" / "stale").write_text("old")
    (template / "was_file").mkdir()
    (template / "was_file" / "new").write_text("new")
    (dest / "was_file").write_text("old")
    (template / "read_only").write_text("new")
    (dest / "read_only").write_text("old")
    (dest / "read_only").chmod(stat.S_IREAD)
    (template / "kept_dir").mkdir()
    (template / "kept_dir" / "fresh").write_text("new")
    (dest / "kept_dir").mkdir()
    (dest / "kept_dir" / "stale").write_text("old")
    seeded.install(template, dest)
    assert (dest / "was_dir").read_text() == "file now"
    assert (dest / "was_file" / "new").read_text() == "new"
    assert (dest / "read_only").read_text() == "new"
    assert sorted(p.name for p in (dest / "kept_dir").iterdir()) == ["fresh"]


def test_a_tampered_record_is_never_removed_and_keeps_the_lane(ready, tmp_path):
    root, d = ready
    prepared = _prepare(root, d)
    temp = Path(prepared.environment["PYTEST_DEBUG_TEMPROOT"])
    elsewhere = tmp_path / "not-a-temp-root"
    elsewhere.mkdir()
    record = root / lane.run_area(prepared.binding) / "pytest-temproot"
    record.write_text(f"{elsewhere}\n")
    assert not lane.clear(root, prepared.binding) and elsewhere.is_dir()
    assert lane.run_busy(root, prepared.binding)
    record.write_text(f"{temp}\n")
    _end(root, prepared)
    assert not temp.exists()


def test_tmpdir_is_unchanged(ready, monkeypatch):
    root, d = ready
    tempfile.gettempdir()
    monkeypatch.setenv("TMPDIR", str(root / "manager-tmp"))
    prepared = _prepare(root, d)
    assert prepared.environment.get("TMPDIR") == str(root / "manager-tmp")
    assert prepared.environment["PYTEST_DEBUG_TEMPROOT"] != prepared.environment.get("TMPDIR")
    _end(root, prepared)


def test_the_verifier_writes_its_temp_root_and_a_telemetry_loss_survives_cleanup(ready, monkeypatch):
    root, d = ready
    tmpdir = root / "manager-tmp"
    tmpdir.mkdir()
    monkeypatch.setenv("TMPDIR", str(tmpdir))
    loss = tmpdir / "agent-telemetry" / "k3-test" / "loss.jsonl"
    # Git Bash shows TMPDIR in POSIX form; cygpath gives back the Windows value it was handed.
    seen = '"$(cygpath -w "$TMPDIR")"' if sys.platform == "win32" else '"$TMPDIR"'
    command = (
        "test " + seen + ' = "' + str(tmpdir) + '" && touch "$PYTEST_DEBUG_TEMPROOT/made-by-pytest" && '
        f'mkdir -p "{loss.parent}" && echo lost > "{loss}"'
    )
    _set_alpha_dispatch(root, verify=[{"command": command, "timeout_seconds": 5}], verify_timeout_seconds=20)
    gate.execute(root, d["id"], 1)
    record = load(root, d["id"])
    value = gate.metadata(record)
    assert value is not None
    binding, _ = value
    status = verification.load_status(root, binding)
    assert status.evidence is not None and status.evidence.state == "complete"
    assert [c.exit_code for c in status.evidence.commands] == [0]
    assert record["run_area"] == lane.run_area(binding)
    area = root / record["run_area"]
    assert not _recorded(root, record).exists() and (area / "terminal.json").exists()
    assert loss.read_text() == "lost\n"


def test_reverify_keeps_its_run_area_on_its_own_row(ready):
    root, d = ready
    blocked = _blocked_by_dirt(root, d)
    row = reverify_mod.reverify(root, d["id"])
    after = load(root, d["id"])
    assert after["run_area"] == blocked["run_area"] != row["run_area"]
    assert row["run_area"] == f".agent/dispatches/{d['id']}.verification/{row['binding']['run_id']}"
    temp = _recorded(root, row)
    assert temp.name.startswith(lane._prefix(Binding(**row["binding"])))
    assert not temp.exists() and root.resolve() not in temp.parents
    assert (root / row["run_area"] / "terminal.json").exists()
