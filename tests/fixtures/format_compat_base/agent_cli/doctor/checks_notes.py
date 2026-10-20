# ruff: noqa
from __future__ import annotations
import json
from pathlib import Path
from .. import notes as notes_mod
from ..core import agent_display
from ..records.narrow import as_dict, str_list
from .check import Check, new_check

SETTINGS = Path(".claude") / "settings.json"
SETTINGS_LOCAL = Path(".claude") / "settings.local.json"


def _json(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return {}
    return as_dict(data)


def check_private_set(root: Path) -> list[Check]:
    out: list[Check] = []
    entries = notes_mod.entries(notes_mod.read_notes_root(root))
    for entry, row in zip(entries, notes_mod.status(root), strict=True):
        name = f"private {entry.path}"
        state = row["state"]
        if state == "linked":
            out.append(new_check(name, "ok", f"linked -> {row['target']}"))
        elif state == "plain":
            what = "directory" if entry.kind == "dir" else "file"
            out.append(
                new_check(
                    name,
                    "fail",
                    f"a plain {what} in this clone; move it into the notes repo and run setup to link it",
                )
            )
        elif state == "diverged":
            out.append(
                new_check(
                    name,
                    "fail",
                    f"no longer the same file as {row['target']} (an atomic replace severed the hard link); run setup: the notes copy wins and this one is parked under {agent_display('scratch', 'notes-diverged')}/",
                )
            )
        elif state == "broken":
            out.append(new_check(name, "fail", f"link target is gone: {row['target']}"))
        elif entry.generated:
            out.append(new_check(name, "warn", "missing; run `agent status render`"))
        elif entry.path == "agent.yaml":
            out.append(new_check(name, "fail", "missing; `agent setup` links it in from the notes repo"))
        else:
            out.append(new_check(name, "warn", "missing; `agent setup` creates it"))
    return out


def check_notes_repo(root: Path) -> list[Check]:
    remembered = notes_mod.read_notes_root(root)
    if remembered is None:
        return [
            new_check("notes repo", "fail", "no notes repo linked; run setup, which creates ../Agent-notes")
        ]
    if not remembered.is_dir():
        return [
            new_check(
                "notes repo", "fail", f"{remembered} (from {agent_display('notes-root')}) is not a directory"
            )
        ]
    gap = _claude_grant_gap(root, remembered.resolve())
    if gap:
        return [new_check("notes repo", "warn", f"{remembered}; {gap}")]
    if _has_no_remote(remembered):
        return [
            new_check(
                "notes repo",
                "warn",
                f"{remembered} has no git remote; fine for one device, but a second device needs it pushed to a private remote (ids are then claimed there)",
            )
        ]
    return [new_check("notes repo", "ok", str(remembered))]


def _has_no_remote(notes_root: Path) -> bool:
    from .. import notes_claims, notes_sync

    if not (notes_root / ".git").exists():
        return False
    return notes_claims.no_remote(notes_sync.Repo(notes_root, notes_sync.LOCAL_CAP_S))


def _claude_grant_gap(root: Path, notes_root: Path) -> str:
    from .. import links as links_mod
    from .. import worktree_grants
    from ..core import AgentError

    if not worktree_grants.claude_set_up(root) or not links_mod.is_link(root / "features"):
        return ""
    try:
        granted = worktree_grants.notes_granted(root, notes_root)
    except AgentError as exc:
        return f"cannot check the Claude grant: {exc.message}"
    if granted:
        return ""
    return f"not a Claude working directory in {SETTINGS_LOCAL.as_posix()}, so reads of features/ prompt; run setup"


def check_notes_sync(root: Path) -> list[Check]:
    from .. import config_file, notes_sync

    if config_file.notes_sync_mode(root) == "off":
        return []
    failure = notes_sync.standing_failure(root)
    if failure:
        level = "fail" if failure.get("status") == "halted" else "warn"
        return [new_check("notes sync", level, f"{failure.get('status')}: {failure.get('detail')}")]
    kept = notes_sync.last_outcomes(root)
    error, ok = (as_dict(kept.get("last_error")), as_dict(kept.get("last_ok")))
    if not error:
        return []
    said = f"{error.get('op')} {error.get('status')} at {error.get('at')}: {error.get('detail')}"
    err_seq, ok_seq = (error.get("seq"), ok.get("seq"))
    if isinstance(err_seq, int) and isinstance(ok_seq, int):
        unrecovered = err_seq > ok_seq
    else:
        unrecovered = str(error.get("at") or "") >= str(ok.get("at") or "")
    if unrecovered:
        return [new_check("notes sync", "warn", f"last sync failed, none has succeeded since: {said}")]
    return [new_check("notes sync", "ok", f"last error {said}; recovered at {ok.get('at')}")]


def enabled_servers(root: Path) -> list[str]:
    out: list[str] = []
    for rel in (SETTINGS, SETTINGS_LOCAL):
        for name in str_list(_json(root / rel).get("enabledMcpjsonServers")):
            if name not in out:
                out.append(name)
    return out
