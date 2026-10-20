"""Install one machine's usage snapshot in the notes repo.

When the linked notes repo has a `.git`, every write happens inside `notes_sync.repo_lock`, in
every `notes_sync` mode, so a session's pull or Stop sync cannot rebase during it. If git cannot
find that repo's lock, nothing is written and the failure comes back for the next run. A notes
folder with no `.git`, or no linked notes repo, has no lock to take and is written directly. The commit
names only the usage paths, so a manager's staged or unsaved notes are never swept into it.
`notes_sync` decides how far it goes: `off` writes the files, `pull` writes and commits, `on`
also publishes through the same path Stop uses.
"""

from __future__ import annotations

from pathlib import Path

from .. import config_file, core, notes_sync
from ..notes import read_notes_root
from .files import base

INSTALL_BUDGET_S = 45.0
LOCK_WAIT_S = 20.0


def _write(where: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        core.atomic_write(where / rel, text)


def install(root: Path, files: dict[str, str], subject: str, body: str) -> notes_sync.Result:
    """`written`, `committed`, `current` or `pushed` when it worked; any other status is a
    failure the caller keeps for the next run."""
    mode = config_file.notes_sync_mode(root)
    opened = notes_sync.open_repo(root, INSTALL_BUDGET_S)
    if isinstance(opened, notes_sync.Result):
        notes = read_notes_root(root)
        if notes is not None and (notes / ".git").exists():
            return notes_sync.Result(
                "error", f"git could not open the notes repo at {notes}; nothing written"
            )
        _write(base(root), files)
        return notes_sync.Result("written", "no notes repo with git; files written, nothing committed")
    repo, gitdir, common = opened
    try:
        with notes_sync.repo_lock(common, min(LOCK_WAIT_S, repo.left())):
            return _locked(repo, gitdir, common, files, subject, body, mode)
    except TimeoutError:
        return notes_sync.Result("locked", "another session holds the notes sync lock")
    except notes_sync._StateError as exc:  # pyright: ignore[reportPrivateUsage]
        return notes_sync.Result("error", str(exc))


def _locked(
    repo: notes_sync.Repo,
    gitdir: Path,
    common: Path,
    files: dict[str, str],
    subject: str,
    body: str,
    mode: str,
) -> notes_sync.Result:
    state = notes_sync._read_state(common)  # pyright: ignore[reportPrivateUsage]
    if state.get("halt"):
        return notes_sync.Result("halted", str(state["halt"].get("detail")))
    if busy := notes_sync._busy(repo, gitdir):  # pyright: ignore[reportPrivateUsage]
        return notes_sync.Result("busy", busy)
    _write(repo.path, files)
    if mode == "off":
        return notes_sync.Result("written")
    rels = sorted(files)
    added = repo.git("add", "--", *rels)
    if added is None or added.returncode:
        return notes_sync.Result("error", f"git add failed: {notes_sync.reason(added)}")
    staged = repo.git("diff", "--cached", "--quiet", "--", *rels)
    if staged is None or staged.returncode not in (0, 1):
        return notes_sync.Result("error", "could not read the staged usage files")
    committed = False
    if staged.returncode == 1:
        c = repo.git("commit", "--quiet", "-m", subject, "-m", body, "--", *rels)
        if c is None or c.returncode:
            return notes_sync.Result("error", f"commit failed: {notes_sync.reason(c)}")
        committed = True
    if mode == "pull":
        return notes_sync.Result("committed" if committed else "current")
    up = notes_sync.upstream_of(repo)
    if isinstance(up, str):
        return notes_sync.Result("config", up)
    counts = notes_sync._counts(repo)  # pyright: ignore[reportPrivateUsage]
    if counts is None:
        return notes_sync.Result("error", "could not count commits against upstream")
    if counts[0] == 0:
        return notes_sync.Result("current")
    return notes_sync._publish(repo, gitdir, common, state, up)  # pyright: ignore[reportPrivateUsage]
