# ruff: noqa
from __future__ import annotations
import platform
from datetime import date as _date
from pathlib import Path
from typing import Any
from .. import doctor as doctor_mod
from .. import frontmatter
from .. import installs as installs_mod
from .. import journal as journal_mod
from .. import links as links_mod
from .. import notes as notes_mod
from .. import obsidian as obsidian_mod
from .. import setup as setup_mod
from ..core import EXIT_NOT_FOUND, EXIT_OK, AgentError, agent_display, table, today
from ..records.narrow import as_list, as_str, dict_list
from ._shared import Op
from .status import obsidian_setup, status_check, status_render


def journal_collect(root: Path, *, date: str | None = None, write: bool = False, as_json: bool = False) -> Op:
    if date:
        try:
            day = _date.fromisoformat(date)
        except ValueError as exc:
            raise AgentError(f"date must be YYYY-MM-DD; got {date!r}") from exc
    else:
        day = today()
    if write:
        journal_mod.require_writer(root)
    bundle = journal_mod.collect(root, day)
    if write:
        path = journal_mod.write(root, bundle, as_json=as_json)
        rel = path.relative_to(root).as_posix()
        return Op({"written": rel, **bundle}, f"wrote {rel}")
    return Op(bundle, journal_mod.render_markdown(bundle))


def journal_missing(root: Path) -> Op:
    missing = [d.isoformat() for d in journal_mod.missing_days(root)]
    stale = [d.isoformat() for d in journal_mod.stale_days(root)]
    text = "\n".join(missing) if missing else "(none)"
    if stale:
        text += "\nstale (run `journal refresh <date>`):\n" + "\n".join(stale)
    return Op({"missing": missing, "stale": stale}, text)


def _parse_day(date: str) -> _date:
    try:
        return _date.fromisoformat(date)
    except ValueError as exc:
        raise AgentError(f"date must be YYYY-MM-DD; got {date!r}") from exc


def journal_write(root: Path, *, date: str, file: str) -> Op:
    journal_mod.require_writer(root)
    day = _parse_day(date)
    src = Path(file)
    if not src.is_file():
        raise AgentError(f"block file not found: {file}")
    text = src.read_text(encoding="utf-8").strip("\n")
    if not text.strip():
        raise AgentError("the block is empty; write `Nothing recorded.` for a quiet day")
    path = journal_mod.write_generated(root, day, text)
    meta, _ = frontmatter.load(path)
    words = len(text.split())
    rel = path.relative_to(root).as_posix()
    data = {
        "date": day.isoformat(),
        "words": words,
        "path": rel,
        "spend_usd": meta.get("spend_usd"),
        "dispatches": meta.get("dispatches"),
        "session_tokens": meta.get("session_tokens"),
        "session_models": meta.get("session_models"),
    }
    from ..telemetry import attention as events

    events.emit(root, "journal.write", date=day.isoformat())
    return Op(data, f"wrote the block of {rel} ({words} words)")


def journal_refresh(root: Path, *, date: str) -> Op:
    journal_mod.require_writer(root)
    day = _parse_day(date)
    path = journal_mod.refresh_numbers(root, day)
    meta, _ = frontmatter.load(path)
    rel = path.relative_to(root).as_posix()
    data = {
        "date": day.isoformat(),
        "path": rel,
        "session_tokens": meta.get("session_tokens"),
        "tokens_complete": meta.get("tokens_complete"),
        "usage_machines": meta.get("usage_machines"),
    }
    return Op(data, f"refreshed the numbers of {rel}")


def doctor(root: Path) -> Op:
    checks = doctor_mod.run(root)
    rows = [[c["level"].upper(), c["check"], c["detail"]] for c in checks]
    code = EXIT_NOT_FOUND if doctor_mod.has_failures(checks) else EXIT_OK
    counts = {level: sum((c["level"] == level for c in checks)) for level in ("ok", "warn", "fail")}
    summary = f"{counts['ok']} OK, {counts['warn']} WARN, {counts['fail']} FAIL"
    return Op(checks, f"{summary}\n{table(rows, ['level', 'check', 'detail'])}", code=code)


def _looks_like_notes_repo(folder: Path) -> bool:
    from .. import config_file

    return notes_mod.is_skeleton(folder) or (folder / config_file.REGISTRY).is_file()


def _notes_step(root: Path, notes: str | None) -> dict[str, Any]:
    remembered = notes_mod.read_notes_root(root)
    target = Path(notes).expanduser() if notes else remembered
    created = ""
    if target is None:
        target = notes_mod.default_notes_root(root)
    if not notes and remembered is not None and (not remembered.exists()):
        return setup_mod.setup_step(
            "notes link",
            "failed",
            f"the notes repo setup linked before, {remembered}, is gone; move it back, or pass --notes <path> to use or create another",
        )
    if not target.exists() or (target.is_dir() and (not notes_mod.occupied(target))):
        try:
            notes_mod.init(target, root=root)
        except AgentError as exc:
            return setup_mod.setup_step("notes link", "failed", exc.message)
        if (target / "agent.yaml").is_file():
            from .. import config_file

            config_file.drop_example_repos(target / "agent.yaml")
        created = f"created {target} (a private git repo; one device needs no remote, a second device needs it pushed somewhere private); "
    elif not _looks_like_notes_repo(target):
        return setup_mod.setup_step(
            "notes link",
            "failed",
            f"{target} is not empty and is not a notes repo (no agent.yaml and no notes README); name an existing notes repo, or a new or empty folder to create one in",
        )
    try:
        rows = notes_mod.link(root, target)
    except AgentError as exc:
        return setup_mod.setup_step("notes link", "failed", exc.message)
    notes_mod.write_notes_root(root, target)
    where = f"{created}linked from {target}"
    refused = [r for r in rows if r["state"] == "refused"]
    if refused:
        return setup_mod.setup_step(
            "notes link", "failed", "; ".join((f"{r['path']}: {r['detail']}" for r in refused))
        )
    done = [r for r in rows if r["state"] == "done"]
    if done:
        return setup_mod.setup_step("notes link", "done", f"{len(done)} of {len(rows)} path(s) {where}")
    return setup_mod.setup_step("notes link", "already", where)


def _codex_config_step(
    root: Path, *, codex_sandbox: str | None = None, codex_inline: bool | None = None
) -> dict[str, Any]:
    notes_root = notes_mod.read_notes_root(root)
    linked_notes = notes_root if links_mod.is_link(root / "features") else None
    state, detail = notes_mod.configure_codex_notes_root(
        root, linked_notes, codex_sandbox=codex_sandbox, codex_inline=codex_inline
    )
    return setup_mod.setup_step("codex manager profile", "failed" if state == "refused" else state, detail)


def _claude_worktree_step(root: Path) -> dict[str, Any]:
    from .. import worktree_grants

    worktree_grants.parent_for(root).mkdir(parents=True, exist_ok=True)
    notes_root = notes_mod.read_notes_root(root)
    linked = notes_root.resolve() if notes_root and links_mod.is_link(root / "features") else None
    state, detail = worktree_grants.configure_claude(root, linked)
    return setup_mod.setup_step("claude worktree grant", "failed" if state == "refused" else state, detail)


def _codex_mcp_step(root: Path) -> dict[str, Any]:
    from .. import codex_mcp

    state, detail = codex_mcp.configure(root)
    return setup_mod.setup_step("codex agent MCP", "failed" if state == "refused" else state, detail)


def _client_skipped(step: str, client: str) -> dict[str, Any]:
    return setup_mod.setup_step(
        step,
        "skipped",
        f"skipped because `{client}` is not installed; to use {client}, install it and run setup again",
    )


def setup(
    root: Path,
    *,
    check_only: bool = False,
    notes: str | None = None,
    install: bool = False,
    owner: str | None = None,
    machine: str | None = None,
    codex_sandbox: str | None = None,
    codex_inline: bool | None = None,
) -> Op:

    def _registry_facts(root: Path) -> dict[str, Any]:
        from .. import repos as repos_mod

        try:
            registry = repos_mod.load_registry(root)
            return {"owner_name": repos_mod.owner_name(root), "repo_names": [r.short for r in registry.repos]}
        except Exception:
            return {"owner_name": "", "repo_names": []}

    system = platform.system()
    rows = setup_mod.checks(system)
    installed: list[dict[str, Any]] = []
    if install and (not check_only):
        todo = installs_mod.installable(rows, system)
        if todo:
            installed = installs_mod.install_missing(todo, system)
            rows = setup_mod.checks(system)
    missing = setup_mod.missing_requirements(rows)
    result: dict[str, Any] = {
        "platform": system,
        "checks": rows,
        "installed": installed,
        "missing": missing,
        "configure": [],
        "doctor": [],
        "ok": not missing,
        **_registry_facts(root),
    }
    if check_only or missing:
        return Op(result, setup_mod.render(result), code=EXIT_OK if not missing else EXIT_NOT_FOUND)
    steps: list[dict[str, Any]] = [
        setup_mod.uv_sync(root),
        setup_mod.caveman_submodule(root),
        setup_mod.skill_links(root),
        installs_mod.terminal_python(root, system),
        setup_mod.git_hooks(root),
        setup_mod.harness_role(root),
        setup_mod.machine_label(root, machine),
        _notes_step(root, notes),
    ]
    notes_ok = steps[-1]["state"] != "failed"
    if notes_ok and owner:
        from .. import config_file

        state, detail = config_file.set_owner_name(root, owner)
        steps.append({"step": "owner name", "state": state, "detail": detail})
    present = {r["name"] for r in rows if r["ok"]}
    if notes_ok and "codex" not in present:
        steps += [
            _client_skipped("codex manager profile", "codex"),
            _client_skipped("codex agent MCP", "codex"),
        ]
    elif notes_ok:
        steps.append(_codex_config_step(root, codex_sandbox=codex_sandbox, codex_inline=codex_inline))
        if steps[-1]["state"] != "failed":
            steps.append(_codex_mcp_step(root))
    if "claude" in present:
        steps.append(_claude_worktree_step(root))
    else:
        from .. import worktree_grants

        worktree_grants.parent_for(root).mkdir(parents=True, exist_ok=True)
        steps.append(_client_skipped("claude worktree grant", "claude"))
    had_obsidian = (root / ".obsidian" / "app.json").exists()
    obsidian_data = obsidian_setup(root).data
    written = as_list(obsidian_data.get("written"))
    failed_plugins = [p for p in dict_list(obsidian_data.get("plugins")) if p.get("state") == "failed"]
    if failed_plugins:
        guides = [
            f"{p['plugin']} {p.get('version', '')}: {p.get('detail', '')}  https://github.com/{obsidian_mod.PLUGIN_SOURCES[as_str(p['plugin'])][0]}/releases"
            for p in failed_plugins
        ]
        steps.append({"step": "obsidian setup", "state": "failed", "detail": "; ".join(guides)})
    else:
        steps.append(
            {
                "step": "obsidian setup",
                "state": "already" if had_obsidian else "done",
                "detail": f"{len(written)} file(s) in .obsidian/",
            }
        )
    fresh = status_check(root).data.get("fresh")
    if not fresh:
        status_render(root)
    steps.append(
        {
            "step": "status render",
            "state": "already" if fresh else "done",
            "detail": f"STATUS.md, adrs/README.md, Dashboard.md, bases/, {agent_display('status.json')}",
        }
    )
    result["configure"] = steps
    result.update(_registry_facts(root))
    doctor_op = doctor(root)
    result["doctor"] = doctor_op.data
    result["incomplete"] = setup_mod.missing_requirements(rows, setup_mod.HARD_REQUIREMENTS)
    result["ok"] = (
        doctor_op.code == EXIT_OK
        and (not any((s["state"] == "failed" for s in steps)))
        and (not result["incomplete"])
    )
    return Op(result, setup_mod.render(result), code=EXIT_OK if result["ok"] else EXIT_NOT_FOUND)
