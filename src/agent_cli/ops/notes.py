"""The notes-repo ops (see ADR-0012): `agent notes init|link|status` over `agent_cli.notes`.

Its own module rather than a corner of `infra`: the private set is a domain, and `setup` and
`doctor` both read it too.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import documents
from .. import notes as notes_mod
from ..core import EXIT_NOT_FOUND, EXIT_OK, agent_display, table
from ._shared import Op


def _rows_table(rows: list[dict[str, Any]], headers: tuple[str, str, str] = ("", "path", "detail")) -> str:
    key = "target" if rows and "target" in rows[0] else "detail"
    return table([[r["state"], r["path"], str(r.get(key) or "")] for r in rows], list(headers))


def notes_link(root: Path, *, path: str) -> Op:
    """Symlink the private set in from the notes repo at `path`, and remember it in
    `.agent/notes-root` so a later `agent setup` with no `--notes` links the same one."""
    rows = notes_mod.link(root, path)
    notes_mod.write_notes_root(root, path)
    refused = [r for r in rows if r["state"] == "refused"]
    text = _rows_table(rows)
    if refused:
        text += "\nnothing was deleted. Move each of those into the notes repo yourself, then re-run."
        return Op(rows, text, code=EXIT_NOT_FOUND)
    state, detail = notes_mod.configure_codex_notes_root(root, path)
    text += f"\nCodex manager profile: {detail}"
    return Op(rows, text, code=EXIT_NOT_FOUND if state == "refused" else EXIT_OK)


def notes_status(root: Path) -> Op:
    """What every private path is in this clone: linked, plain, missing or broken."""
    rows = notes_mod.status(root)
    linked = sum(1 for r in rows if r["state"] == "linked")
    text = _rows_table(rows, ("", "path", "target"))
    remembered = notes_mod.read_notes_root(root)
    text += f"\n{linked}/{len(rows)} linked"
    text += (
        f"; notes repo {remembered}"
        if remembered
        else f"; no notes repo ({agent_display('notes-root')} is unset)"
    )
    return Op(rows, text)


def notes_publish(root: Path) -> Op:
    """Commit and push the notes repo now, and name the commit that holds it. A machine handing
    evidence to another one runs this first, then sends the path
    with `notes-commit: <sha>`; the reader's prompt hook pulls until it has that commit. Anything
    but a published result means do not send a ready reference yet: the file stays dirty and is
    retried at the next Stop."""
    from .. import notes_sync
    from ..hooks.notes_sync import refresh_views

    r = notes_sync.sync(root)
    refresh_views(root, r, "publish")  # its rebase may have brought commits in
    # The commit the sync verified equal to upstream while it held the lock, never a HEAD read
    # afterwards: a commit made in between is not published.
    sha = r.head
    published = r.status in ("pushed", "current") and r.converged and bool(sha)
    data = {
        "status": r.status,
        "detail": r.detail,
        "commit": sha if published else None,
        "published": published,
    }
    text = (
        f"notes published; reference it as `notes-commit: {sha}`"
        if published
        else f"notes not published ({r.status}{': ' + r.detail if r.detail else ''}); do not send a ready reference yet"
    )
    return Op(data, text)


def notes_root(root: Path) -> Op:
    """Return the repository owning a private path: the linked notes repo, or the harness for plain
    files. Stage private changes there because git cannot stage through symbolic links from the
    public vault.
    """
    remembered = notes_mod.read_notes_root(root)
    linked = any(r["state"] == "linked" for r in notes_mod.status(root))
    target = remembered if (remembered and linked) else root
    resolved = Path(target).resolve()
    payload = {
        "root": str(resolved),
        "linked": bool(remembered and linked),
        "harness_root": str(root.resolve()),
    }
    return Op(payload, str(resolved))


def notes_document(
    root: Path,
    *,
    target: str,
    from_file: str,
    apply: bool = False,
    expect_sha256: str | None = None,
    create: bool = False,
    expect_absent: bool = False,
) -> Op:
    """Preview or explicitly apply a scoped document replacement with stale-content checks."""
    result = documents.edit(
        root,
        target=target,
        source=Path(from_file),
        apply=apply,
        expect_sha256=expect_sha256,
        create=create,
        expect_absent=expect_absent,
    )
    state = "applied" if apply else "preview only"
    expectation = result["before_sha256"] or "absent (--create --expect-absent)"
    text = (
        f"{state}: {target}\nresolved target: {result['resolved_target']}\nexpected SHA-256: {expectation}\n"
    )
    text += result["diff"] or "(no changes)\n"
    return Op(result, text)


def notes_write(
    root: Path,
    *,
    target: str,
    from_file: str | None = None,
    apply: bool = False,
    expect_sha256: str | None = None,
) -> Op:
    """Draft, preview or apply a change to a linked private file, keeping its link."""
    result = notes_mod.write_file(
        root,
        target=target,
        source=Path(from_file) if from_file else None,
        apply=apply,
        expect_sha256=expect_sha256,
    )
    if "draft" in result:
        text = (
            f"draft of {target}: {result['draft']}\nedit the draft, then preview it with from_file; "
            f"expected SHA-256: {result['before_sha256']}\n"
        )
        return Op(result, text)
    state = "applied" if result["applied"] else "preview only"
    text = f"{state}: {target} (link: {result['link']})\nexpected SHA-256: {result['before_sha256']}\n"
    text += result["diff"] or "(no changes)\n"
    return Op(result, text)


def mcp_servers_install(root: Path, *, update: bool = False) -> Op:
    """Install declared MCP servers and probe startup. A valid-looking pin does not prove an
    executable server or healthy package cache.
    """
    from .. import mcp_servers

    result = mcp_servers.install(root, update=update)
    if not result["installed"]:
        return Op(
            result,
            f"no MCP server manifest at {result['dir']}/package.json; nothing to install",
        )
    probes = {name: mcp_servers.probe(path) for name, path in mcp_servers.binaries(root).items()}
    result["probes"] = {name: {"ok": ok, "detail": detail} for name, (ok, detail) in probes.items()}
    broken = [f"{name} ({detail})" for name, (ok, detail) in probes.items() if not ok]
    pkgs = ", ".join(f"{k}@{v}" for k, v in sorted(result["packages"].items())) or "nothing"
    text = f"installed {pkgs} into {result['dir']}"
    if broken:
        text += f"; DOES NOT START: {'; '.join(broken)}"
    elif probes:
        text += f"; {len(probes)} binary(ies) start"
    return Op(result, text)


def notes_init(root: Path, *, path: str) -> Op:
    """Build a fresh notes repo skeleton at `path`, ready for `agent setup --notes <path>`."""
    rows = notes_mod.init(path, root=root)
    text = _rows_table(rows)
    text += f"\nnow link it: `agent setup --notes {path}`"
    return Op(rows, text)
