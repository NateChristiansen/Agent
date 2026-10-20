"""Set Terminal userTitle to represent manager obligations independently of client glyphs.
Session-title hooks run only at prompt boundaries, while Terminal relabeling can update a running
tab.

Match session names in displayed or previously set labels. Confirm the first Codex claim, then
batch later paints through tab_drain so managers share one bounded eval stream. AGENT_NOW and
AGENT_NO_TABS disable writes for tests and explicit opt-out.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from .core import agent_path

WAITING_ON_YOU = "✋"
WAITING_ON_WORKER = "⏳"
CODEX_STARTING_TITLE = "✅ Codex manager"
CLAUDE_STARTING_TITLE = "✅ Claude manager"
WORKING = "⚙️"
AT_REST_LABEL = "✅"
MAX_TAB_NAME_CHARS = 18
CLAIM_LOCK_WAIT_S = 2.0
"""The longest a first claim waits for another eval (`tab_drain.EVAL_LOCK`); it runs in a hook."""


def short_name(session_id: str, provider: str) -> str:
    """The form the owner reads off a manager tab and uses to name it: `cl 834e`, `cx 8ddb`.
    `peer list` shows it and `peer send` resolves it."""
    short = session_id[-8:-4] if provider == "codex" else session_id[:4]
    return f"{'cx' if provider == 'codex' else 'cl'} {short}"


def manager_identity(session_id: str, provider: str) -> str:
    """Stable suffix shared by every painted state of one manager tab."""
    return f" · {short_name(session_id, provider)}"


def manager_display_name(name: str, session_id: str, provider: str, *, generated: bool) -> str:
    """Shorten only the tab text; the client and peer session names stay intact."""
    prefix = ""
    if generated:
        base = name.removeprefix("mgr-")
        if provider == "codex":
            base = base.removesuffix(f"-codex-{session_id[-8:]}")
        elif not base.startswith("unmapped-"):
            base = base.removesuffix(f"-{session_id[:6]}")  # a second manager's name
        display = "new" if base.startswith("unmapped-") or base.startswith("codex-") else base
        if len(display) > 3 and display[:3].isdigit() and display[3] == "-":
            prefix, display = display[:3] + " ", display[4:]
    else:
        display = name
    if len(display) > MAX_TAB_NAME_CHARS:
        display = display[: MAX_TAB_NAME_CHARS - 1].rstrip("-_ ") + "…"
    return prefix + display + manager_identity(session_id, provider)


_COUNT = re.compile(r"\+\d+ ")


def label_name(label: str) -> str:
    """The name inside a label `label_for` made: after the glyph and any `+N` count."""
    _, _, rest = label.partition(" ")
    if match := _COUNT.match(rest):
        rest = rest[match.end() :]
    return rest.strip()


def label_for(
    name: str,
    state: str | None,
    running_workers: list[str],
    background: int = 0,
    needs_owner: list[str] | tuple[str, ...] = (),
    manager_action: int = 0,
) -> str:
    """Choose one provider-independent tab glyph: owner obligation first, then running workers or
    background tasks, then working, then idle. Finished results belong in Dashboard manager
    obligations, not the tab count. Accept `manager_action` for compatibility; `label_name` still
    reads old +N labels for matching.
    """
    if state == "waiting_on_you" or needs_owner:
        glyph = WAITING_ON_YOU
    elif state == "idle" and (running_workers or background):
        glyph = WAITING_ON_WORKER
    elif state == "working":
        glyph = WORKING
    else:
        glyph = AT_REST_LABEL
    return f"{glyph} {name}"


STALE_MARK = "?"
STALE_READ_INTERVAL_S = 2.0


class StaleReads:
    """Confirm expiry on consecutive polls; a fresh read clears the candidate immediately."""

    def __init__(self) -> None:
        self.entries: dict[str, tuple[str, int, float]] = {}

    def confirm(self, key: str, label: str, stale: bool, now: float, required: int = 3) -> bool:
        if not stale:
            self.entries.pop(key, None)
            return False
        previous, count, at = self.entries.get(key, (label, 0, float("-inf")))
        if previous != label:
            count, at = 0, float("-inf")
        if now - at >= STALE_READ_INTERVAL_S:
            count, at = count + 1, now
        self.entries[key] = (label, count, at)
        return count >= required


def stale_label(label: str) -> str:
    """Append `?` to the last glyph after snapshot expiry, preserving its name and matching layout.
    Quiet tabs often remain correct; mark the observation unconfirmed rather than replace every
    label with noise.
    """
    glyph, sep, rest = label.partition(" ")
    if not sep or glyph.endswith(STALE_MARK):
        return label
    return f"{glyph}{STALE_MARK} {rest}"


def eval_code(
    name: str,
    label: str,
    match_generated: bool = True,
    *,
    claim_active: bool = False,
    claim_untitled: bool = False,
    resume_display: str = "",
    provisional_title: str = "",
    launch_marker: str = "",
) -> str:
    """Generate an Obsidian eval that sets `userTitle` on tabs matched by name or displayed title. Use
    `leaf.getDisplayText()` for generated labels: Terminal's view state may leave `titleGenerated`
    null even while the tab displays a title.
    """
    # Claude's `mgr-NNN-slug` is a prefix of a Codex manager's
    # `mgr-NNN-slug-codex-<id>`. Substring matching changed both tabs on every Claude
    # transition and erased the Codex tab's distinct title. Match the entire name after
    # the client's glyph instead.
    shown = (
        "(String((leaf.getDisplayText && leaf.getDisplayText()) || '') === name || "
        "String((leaf.getDisplayText && leaf.getDisplayText()) || '').endsWith(' ' + name)) || "
    )
    provisional = (
        f" || ((!s.userTitle || [{json.dumps(provisional_title)}, {json.dumps(provisional_title.replace('✅', '⏳'))}].includes(s.userTitle)) && "
        "String((s.profile && s.profile.args || []).join(' '))"
        # The marker alone: the POSIX profile writes `AGENT_RIBBON_LAUNCH=<m> `, PowerShell
        # `$env:AGENT_RIBBON_LAUNCH='<m>';`. It ends in a random UUID, so it cannot collide.
        f".includes({json.dumps(launch_marker)}))"
        if claim_active and provisional_title and launch_marker
        else ""
    )
    claim = (
        " || (leaf === app.workspace.activeLeaf && "
        "((!s.userTitle && (String(leaf.getDisplayText()).includes('| Agent') || "
        f"{str(claim_untitled).lower()})) || "
        f"({json.dumps(bool(resume_display))} && String(s.userTitle || '').endsWith({json.dumps(resume_display)})) || "
        "String(s.userTitle || '').endsWith(' ' + name)))"
        if claim_active and not launch_marker
        else ""
    )
    diagnostic_vars = (
        "let activeTerminal = false; let agentTitle = false; let activeDisplay = ''; " if claim_active else ""
    )
    diagnostic_check = (
        "if (leaf === app.workspace.activeLeaf) { activeTerminal = true; "
        "activeDisplay = encodeURIComponent(String(leaf.getDisplayText())); "
        "agentTitle = String(leaf.getDisplayText()).includes('| Agent'); } "
        if claim_active
        else ""
    )
    diagnostic_result = (
        " + ' active_terminal=' + activeTerminal + ' agent_title=' + agentTitle"
        " + ' active_display=' + activeDisplay"
        if claim_active
        else ""
    )
    relabel = (
        f"if ((s.userTitle || '') === {json.dumps(label)}) continue; "
        f"const c = structuredClone(s); c.userTitle = {json.dumps(label)}; v.state = c; n += 1; }} "
    )
    if claim_active:
        # A first claim changes one tab or none. Two leaves with the same marker (a duplicated or
        # restored tab) are refused before either title changes.
        body = (
            "hits.push(leaf); } "
            "if (hits.length === 1) for (const leaf of hits) { const v = leaf.view; const s = v.state; "
            f"{relabel}"
        )
        result = f"'relabeled ' + n + ' hits=' + hits.length{diagnostic_result}"
    else:
        body = relabel
        result = "'relabeled ' + n"
    return (
        "(() => { let n = 0; const hits = []; "
        f"{diagnostic_vars}"
        "for (const leaf of app.workspace.getLeavesOfType('terminal:terminal')) { "
        "const v = leaf.view; const s = v && v.state; if (!s) continue; "
        f"{diagnostic_check}"
        f"const name = {json.dumps(name)}; "
        f"const mine = {shown if match_generated else ''}"
        "(String(s.userTitle || '') === name || String(s.userTitle || '').endsWith(' ' + name))"
        f"{claim}{provisional}; "
        "if (!mine) continue; "
        f"{body}"
        f"return {result}; }})()"
    )


TABS_OFF_FILE = agent_path(Path(), "tabs-off")
"""A shared file disables relabeling for every session on its next hook event, without a client
restart. Per-process AGENT_NO_TABS cannot stop concurrent painters. Repeated eval paints can thrash
xterm's WebGL glyph atlas, so retain both the shared rate gate and this immediate kill switch.
"""


def enabled(root: Path | None = None) -> bool:
    if os.environ.get("AGENT_NOW") or os.environ.get("AGENT_NO_TABS"):
        return False
    # No cwd fallback. `bin/agent` exports `AGENT_ROOT`, so a real hook always has one; falling
    # back to `.` instead made an isolated test read the developer's own repo and see a switch
    # it never set.
    base = root or (Path(env) if (env := os.environ.get("AGENT_ROOT")) else None)
    if base is None:
        return True
    try:
        return not (base / TABS_OFF_FILE).exists()
    except OSError:
        return True


def _claim_failure(name: str, reason: str) -> None:
    """Keep failed first-claim reasons so a late title has an inspectable cause."""
    root = os.environ.get("AGENT_ROOT")
    if not root:
        return
    try:
        path = agent_path(Path(root), "tabs", "claim-failures.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": time.time(), "name": name, "reason": reason}) + "\n")
    except OSError:
        pass


def _record_early_claim(name: str, output: str) -> None:
    """Leave evidence of which active tab was claimed before Codex supplied its title."""
    root = os.environ.get("AGENT_ROOT")
    if not root or "agent_title=false" not in output:
        return
    try:
        encoded = output.split(" active_display=", 1)[1].split()[0]
        display = unquote(encoded)[:120]
        path = agent_path(Path(root), "tabs", "claims.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": time.time(), "name": name, "active_display": display}) + "\n")
    except OSError, IndexError:
        pass


def claim_active(
    name: str,
    label: str,
    *,
    allow_untitled: bool = False,
    resume_display: str = "",
    provisional_title: str = "",
    launch_marker: str = "",
) -> bool:
    """Claim an active unnamed tab or a ribbon tab with this launch marker; confirm one relabel.

    A Codex title does not carry our manager name until this first write. Later updates use
    the shared drainer, which matches that unique name and enforces the aggregate rate gate.
    """
    if not enabled():
        _claim_failure(name, "disabled")
        return False
    from .dispatch.opener import obsidian_cli

    exe = obsidian_cli()
    if exe is None:
        _claim_failure(name, "obsidian-cli-unavailable")
        return False
    from .tab_drain import run_eval

    reason = "launch-tab-not-found" if launch_marker else "no-tab-relabelled"
    attempts = 1 if allow_untitled else 3
    root = Path(env) if (env := os.environ.get("AGENT_ROOT")) else None
    code = eval_code(
        name,
        label,
        claim_active=True,
        claim_untitled=allow_untitled,
        resume_display=resume_display,
        provisional_title=provisional_title,
        launch_marker=launch_marker,
    )
    for attempt in range(attempts):
        # Under the drainer's eval lock, so a claim never runs beside a batch;
        # never against a closed app, which the CLI would start on this hook's pipes.
        run = run_eval(root, code, 5, source="claim", wait=CLAIM_LOCK_WAIT_S)
        if run.ended == "timeout":
            _claim_failure(name, "obsidian-cli-timeout")
            return False
        if run.ended == "oserror":
            _claim_failure(name, f"obsidian-cli-error:{run.stderr.partition(':')[0]}")
            return False
        if run.ended in ("not-running", "lock-busy", "no-cli"):
            _claim_failure(name, {"not-running": "obsidian-not-running"}.get(run.ended, run.ended))
            return False
        if run.stdout is None:
            _claim_failure(name, f"obsidian-cli-exit:{run.returncode}")
            return False
        if re.search(r"(?:^|\s)relabeled 1(?:\s|$)", run.stdout):
            if allow_untitled and not launch_marker:
                _record_early_claim(name, run.stdout)
            return True
        hits = re.search(r"hits=(\d+)", run.stdout)
        if hits and int(hits.group(1)) > 1:
            reason = f"ambiguous-tabs:{hits.group(1)}"
        elif not launch_marker and "active_terminal=false" in run.stdout:
            reason = "no-active-terminal"
        elif not launch_marker and "agent_title=false" in run.stdout:
            reason = "active-terminal-title-not-ready"
        if attempt < attempts - 1:
            time.sleep(0.35)
    _claim_failure(name, f"{reason}; attempts={attempts}")
    return False


RUNNING = "◐"
AT_REST = "✳"


def worker_label(dispatch_id: str, state: str | None) -> str:
    """The glyphs Claude Code draws on an interactive tab, applied to a watch tab, which has no
    Claude title of its own: ◐ while the worker runs, ✋ when it needs the owner (question,
    blocked, failed), ✳ otherwise."""
    name = f"{dispatch_id} watch"
    if state in ("queued", "running"):
        return f"{RUNNING} {name}"
    if state in ("question", "blocked", "failed"):
        return f"{WAITING_ON_YOU} {name}"
    return f"{AT_REST} {name}"


def relabel_worker(root: Path, dispatch_id: str, state: str | None) -> dict[str, Any]:
    """Relabel a worker's watch tab from its dispatch state. The tab is found by
    the `<id> watch` label `dispatch open` gave it."""
    from .tab_drain import request

    label = worker_label(dispatch_id, state)
    return request(root, "watch", dispatch_id, f"{dispatch_id} watch", label, generated=False)
