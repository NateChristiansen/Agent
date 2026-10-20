# ruff: noqa
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
"The longest a first claim waits for another eval (`tab_drain.EVAL_LOCK`); it runs in a hook."


def short_name(session_id: str, provider: str) -> str:
    short = session_id[-8:-4] if provider == "codex" else session_id[:4]
    return f"{('cx' if provider == 'codex' else 'cl')} {short}"


def manager_identity(session_id: str, provider: str) -> str:
    return f" · {short_name(session_id, provider)}"


def manager_display_name(name: str, session_id: str, provider: str, *, generated: bool) -> str:
    prefix = ""
    if generated:
        base = name.removeprefix("mgr-")
        if provider == "codex":
            base = base.removesuffix(f"-codex-{session_id[-8:]}")
        elif not base.startswith("unmapped-"):
            base = base.removesuffix(f"-{session_id[:6]}")
        display = "new" if base.startswith("unmapped-") or base.startswith("codex-") else base
        if len(display) > 3 and display[:3].isdigit() and (display[3] == "-"):
            prefix, display = (display[:3] + " ", display[4:])
    else:
        display = name
    if len(display) > MAX_TAB_NAME_CHARS:
        display = display[: MAX_TAB_NAME_CHARS - 1].rstrip("-_ ") + "…"
    return prefix + display + manager_identity(session_id, provider)


_COUNT = re.compile("\\+\\d+ ")


def label_name(label: str) -> str:
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
    def __init__(self) -> None:
        self.entries: dict[str, tuple[str, int, float]] = {}

    def confirm(self, key: str, label: str, stale: bool, now: float, required: int = 3) -> bool:
        if not stale:
            self.entries.pop(key, None)
            return False
        previous, count, at = self.entries.get(key, (label, 0, float("-inf")))
        if previous != label:
            count, at = (0, float("-inf"))
        if now - at >= STALE_READ_INTERVAL_S:
            count, at = (count + 1, now)
        self.entries[key] = (label, count, at)
        return count >= required


def stale_label(label: str) -> str:
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
    shown = "(String((leaf.getDisplayText && leaf.getDisplayText()) || '') === name || String((leaf.getDisplayText && leaf.getDisplayText()) || '').endsWith(' ' + name)) || "
    provisional = (
        f" || ((!s.userTitle || [{json.dumps(provisional_title)}, {json.dumps(provisional_title.replace('✅', '⏳'))}].includes(s.userTitle)) && String((s.profile && s.profile.args || []).join(' ')).includes({json.dumps(launch_marker)}))"
        if claim_active and provisional_title and launch_marker
        else ""
    )
    claim = (
        f" || (leaf === app.workspace.activeLeaf && ((!s.userTitle && (String(leaf.getDisplayText()).includes('| Agent') || {str(claim_untitled).lower()})) || ({json.dumps(bool(resume_display))} && String(s.userTitle || '').endsWith({json.dumps(resume_display)})) || String(s.userTitle || '').endsWith(' ' + name)))"
        if claim_active and (not launch_marker)
        else ""
    )
    diagnostic_vars = (
        "let activeTerminal = false; let agentTitle = false; let activeDisplay = ''; " if claim_active else ""
    )
    diagnostic_check = (
        "if (leaf === app.workspace.activeLeaf) { activeTerminal = true; activeDisplay = encodeURIComponent(String(leaf.getDisplayText())); agentTitle = String(leaf.getDisplayText()).includes('| Agent'); } "
        if claim_active
        else ""
    )
    diagnostic_result = (
        " + ' active_terminal=' + activeTerminal + ' agent_title=' + agentTitle + ' active_display=' + activeDisplay"
        if claim_active
        else ""
    )
    relabel = f"if ((s.userTitle || '') === {json.dumps(label)}) continue; const c = structuredClone(s); c.userTitle = {json.dumps(label)}; v.state = c; n += 1; }} "
    if claim_active:
        body = f"hits.push(leaf); }} if (hits.length === 1) for (const leaf of hits) {{ const v = leaf.view; const s = v.state; {relabel}"
        result = f"'relabeled ' + n + ' hits=' + hits.length{diagnostic_result}"
    else:
        body = relabel
        result = "'relabeled ' + n"
    return f"(() => {{ let n = 0; const hits = []; {diagnostic_vars}for (const leaf of app.workspace.getLeavesOfType('terminal:terminal')) {{ const v = leaf.view; const s = v && v.state; if (!s) continue; {diagnostic_check}const name = {json.dumps(name)}; const mine = {(shown if match_generated else '')}(String(s.userTitle || '') === name || String(s.userTitle || '').endsWith(' ' + name)){claim}{provisional}; if (!mine) continue; {body}return {result}; }})()"


TABS_OFF_FILE = agent_path(Path(), "tabs-off")
"A shared file disables relabeling for every session on its next hook event, without a client\nrestart. Per-process AGENT_NO_TABS cannot stop concurrent painters. Repeated eval paints can thrash\nxterm's WebGL glyph atlas, so retain both the shared rate gate and this immediate kill switch.\n"


def enabled(root: Path | None = None) -> bool:
    if os.environ.get("AGENT_NOW") or os.environ.get("AGENT_NO_TABS"):
        return False
    base = root or (Path(env) if (env := os.environ.get("AGENT_ROOT")) else None)
    if base is None:
        return True
    try:
        return not (base / TABS_OFF_FILE).exists()
    except OSError:
        return True


def _claim_failure(name: str, reason: str) -> None:
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
        if re.search("(?:^|\\s)relabeled 1(?:\\s|$)", run.stdout):
            if allow_untitled and (not launch_marker):
                _record_early_claim(name, run.stdout)
            return True
        hits = re.search("hits=(\\d+)", run.stdout)
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
    name = f"{dispatch_id} watch"
    if state in ("queued", "running"):
        return f"{RUNNING} {name}"
    if state in ("question", "blocked", "failed"):
        return f"{WAITING_ON_YOU} {name}"
    return f"{AT_REST} {name}"


def relabel_worker(root: Path, dispatch_id: str, state: str | None) -> dict[str, Any]:
    from .tab_drain import request

    label = worker_label(dispatch_id, state)
    return request(root, "watch", dispatch_id, f"{dispatch_id} watch", label, generated=False)
