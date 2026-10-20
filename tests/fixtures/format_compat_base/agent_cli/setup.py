# ruff: noqa
from __future__ import annotations
import platform
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from .core import table
from .records.narrow import as_list, dict_list, str_list
from .setup_guidance import fresh_clone_warnings as fresh_clone_warnings
from .setup_tools import CLAUDE_NPM as CLAUDE_NPM
from .setup_tools import MACOS as MACOS
from .setup_tools import OBSIDIAN_APP as OBSIDIAN_APP
from .setup_tools import TOOLS as TOOLS
from .setup_tools import WINDOWS as WINDOWS
from .setup_tools import WINGET as WINGET
from .setup_tools import Tool as Tool

VENDOR_CAVEMAN = Path("vendor") / "caveman"
CAVEMAN_SPARSE = (
    "git -C vendor/caveman sparse-checkout set --no-cone /skills/caveman/ /src/hooks/ /LICENSE /README.md"
)
PYTHON_MIN = (3, 14)
HARD_REQUIREMENTS = ("git", "uv", "python", "node", "claude", "codex", "obsidian app", "obsidian")
GATE_REQUIREMENTS = ("git", "uv", "python", "node", "claude", "codex")
CLIENTS = ("claude", "codex")


def _row(name: str, ok: bool, found: str, guide: str) -> dict[str, Any]:
    return {"name": name, "ok": ok, "found": found, "guide": guide}


def run_ok(argv: Sequence[str], cwd: Path | None = None) -> bool:
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=60)
    except OSError:
        return False
    except subprocess.SubprocessError:
        return False
    return proc.returncode == 0


def python_row() -> dict[str, Any]:
    version = platform.python_version()
    ok = sys.version_info[:2] >= PYTHON_MIN
    minimum = ".".join((str(p) for p in PYTHON_MIN))
    return _row(
        "python",
        ok,
        f"{version} ({sys.executable})",
        "" if ok else f"uv sync installs python >= {minimum} for this project  https://docs.astral.sh/uv/",
    )


def checks(system: str | None = None) -> list[dict[str, Any]]:
    system = system or platform.system()
    from . import installs

    installs.refresh_windows_path()
    rows: list[dict[str, Any]] = []
    from .obsidian import cli_row

    for tool in TOOLS:
        if tool.name == "obsidian":
            rows.append(cli_row(tool.name, tool.guide(system)))
            continue
        found = shutil.which(tool.binary)
        rows.append(_row(tool.name, bool(found), found or "", "" if found else tool.guide(system)))
        if tool.name == "uv":
            rows.append(python_row())
        if tool.name == "gh" and found:
            authed = run_ok(["gh", "auth", "status"])
            rows.append(
                _row(
                    "gh auth",
                    authed,
                    "signed in" if authed else "not signed in",
                    "" if authed else "gh auth login",
                )
            )
    app = installs.obsidian_app_found(system)
    rows.append(_row(OBSIDIAN_APP.name, bool(app), app, "" if app else OBSIDIAN_APP.guide(system)))
    if system == WINDOWS:
        from . import powershell_policy

        effective, scopes, error = powershell_policy.query()
        cmd_works = powershell_policy.cmd_shim_works() if effective else False
        detail = powershell_policy.guidance(effective, scopes, cmd_works=cmd_works) if effective else error
        rows.append(
            _row(
                "PowerShell policy",
                powershell_policy.codex_launch_ok(effective, cmd_works=cmd_works),
                effective or "unverified",
                detail,
            )
        )
    return rows


def client_found(rows: list[dict[str, Any]]) -> bool:
    return any((r["ok"] for r in rows if r["name"] in CLIENTS))


def missing_requirements(rows: list[dict[str, Any]], names: Sequence[str] = GATE_REQUIREMENTS) -> list[str]:
    either = client_found(rows)
    return [
        r["name"]
        for r in rows
        if r["name"] in names and (not r["ok"]) and (not (either and r["name"] in CLIENTS))
    ]


def setup_step(step: str, state: str, detail: str = "") -> dict[str, Any]:
    return {"step": step, "state": state, "detail": detail}


def harness_role(root: Path) -> dict[str, Any]:
    from . import harness_role as role_mod

    state, detail = role_mod.create_local(root)
    return setup_step("harness role", state, detail)


def machine_label(root: Path, label: str | None) -> dict[str, Any]:
    from .dispatch import machine as machine_mod

    state, detail = machine_mod.set_label(root, label or machine_mod.default_label())
    return setup_step("machine label", state, detail)


def uv_sync(root: Path) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["uv", "sync", "--project", str(root)], capture_output=True, text=True, timeout=600
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return setup_step("uv sync", "failed", str(exc))
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return setup_step("uv sync", "failed", tail[-1] if tail else f"exit {proc.returncode}")
    noise = proc.stderr or ""
    changed = any((word in noise for word in ("Installed", "Uninstalled", "Prepared", "Creating virtual")))
    return setup_step("uv sync", "done" if changed else "already", "the project venv is in sync")


def caveman_submodule(root: Path) -> dict[str, Any]:
    sub = root / VENDOR_CAVEMAN
    populated = sub.exists() and any(sub.iterdir())
    if populated and (not (sub / "CLAUDE.md").exists()):
        return setup_step("caveman submodule", "already", "initialised and sparse")
    if not populated and (
        not run_ok(["git", "submodule", "update", "--init", str(VENDOR_CAVEMAN)], cwd=root)
    ):
        return setup_step("caveman submodule", "failed", f"git submodule update --init {VENDOR_CAVEMAN}")
    if not run_ok(CAVEMAN_SPARSE.split(), cwd=root):
        return setup_step("caveman submodule", "failed", CAVEMAN_SPARSE)
    return setup_step("caveman submodule", "done", "initialised, sparse checkout applied")


def skill_links(root: Path) -> dict[str, Any]:
    from . import skill_links as links_mod

    rows = links_mod.ensure(root)
    bad = [r for r in rows if r["state"] in ("failed", "refused")]
    if bad:
        return setup_step(
            "skill links", "failed", "; ".join((f"{r['link']}: {r.get('detail', '')}" for r in bad))
        )
    made = [r for r in rows if r["state"] in ("created", "replaced")]
    skipped = [r["link"] for r in rows if r["state"] == "skipped"]
    note = f"; skipped (target missing): {', '.join(skipped)}" if skipped else ""
    if not made:
        return setup_step("skill links", "already", f"{len(rows) - len(skipped)} link(s) in place{note}")
    return setup_step("skill links", "done", f"{len(made)} of {len(rows)} link(s) made{note}")


def git_hooks(root: Path) -> dict[str, Any]:
    from . import gitgate
    from .core import AgentError

    if not (root / ".git").exists():
        return setup_step("git hooks", "already", "not a git checkout")
    try:
        result = gitgate.install(root)
    except (AgentError, OSError) as exc:
        return setup_step("git hooks", "failed", str(exc))
    detail = ", ".join((f"{name} {state}" for name, state in result.items()))
    if any(("foreign" in state for state in result.values())):
        return setup_step("git hooks", "failed", detail)
    changed = any((state in ("installed", "updated") for state in result.values()))
    state = "done" if changed else "already"
    export_state, export_detail = gitgate.settle_export_only(root)
    if export_state == "done":
        state = "done"
    return setup_step("git hooks", state, f"{detail}; {export_detail}")


def render(result: dict[str, Any]) -> str:
    lines = [f"platform: {result['platform']}"]
    checks = dict_list(result["checks"])
    either = client_found(checks)

    def label(name: str) -> tuple[bool, str]:
        if name in CLIENTS:
            other = next((c for c in CLIENTS if c != name))
            return (not either, f"{name} (or {other})")
        return (name in HARD_REQUIREMENTS, name + ("" if name in HARD_REQUIREMENTS else " (optional)"))

    rows: list[list[str]] = []
    for r in checks:
        hard, name = label(str(r["name"]))
        state = "ok" if r["ok"] else "MISSING" if hard else "absent"
        rows.append([state, name, str(r["found"]), str(r["guide"])])
    lines.append(table(rows, ["", "requirement", "found", "install"]))
    if result.get("installed"):
        lines.append("")
        lines.append(
            table(
                [[r["state"], r["tool"], r["detail"]] for r in result["installed"]], ["", "install", "detail"]
            )
        )
    if result["missing"]:
        lines.append("missing hard requirement(s): " + ", ".join(result["missing"]))
        lines.append("install them with the lines above, then run this again.")
        if set(CLIENTS) <= set(result["missing"]):
            lines.append(f"One client is enough: install {' or '.join(CLIENTS)}, whichever you use.")
    if result["configure"]:
        lines.append("")
        lines.append(
            table(
                [[c["state"], c["step"], c["detail"]] for c in result["configure"]],
                ["", "configure", "detail"],
            )
        )
    if result["doctor"]:
        lines.append("")
        lines.append(
            table(
                [[c["level"].upper(), c["check"], c["detail"]] for c in result["doctor"]],
                ["level", "check", "detail"],
            )
        )
    lines.append("")
    lines += next_steps(result)
    return "\n".join(lines)


PLACEHOLDER_REPO = "example-service"


def next_steps(result: dict[str, Any]) -> list[str]:
    if result["missing"]:
        return [
            "Next: install the missing requirement(s) above, then run setup again (./setup.sh, or setup.cmd on Windows).",
            "Nothing was configured, because configuring against a missing tool reports worse errors than this list does.",
        ]
    failed = [str(c["step"]) for c in dict_list(result.get("configure")) if c.get("state") == "failed"]
    incomplete = str_list(result.get("incomplete"))
    doctor_failed = [str(c["check"]) for c in dict_list(result.get("doctor")) if c.get("level") == "fail"]
    if result.get("ok") and (not (failed or incomplete or doctor_failed)):
        lines = ["Setup is complete."]
    else:
        lines = ["Setup is incomplete."]
        if incomplete:
            lines.append(
                f"  Still missing: {', '.join(incomplete)}. Install with the line above, then run setup again."
            )
        if failed:
            lines.append(f"  Failed steps: {', '.join(failed)}. Their detail above says what to do.")
        if doctor_failed:
            lines.append(f"  Doctor failures: {', '.join(doctor_failed)}. See the doctor table above.")
    skipped = [str(c["step"]) for c in dict_list(result.get("configure")) if c.get("state") == "skipped"]
    if skipped:
        lines.append(f"  Skipped: {', '.join(skipped)}. Their detail above says why.")
    owner = result.get("owner_name")
    repos = as_list(result.get("repo_names"))
    steps: list[str] = []
    if not owner:
        steps.append(
            "Put your name in agent.yaml: `owner: {name: ...}` is unset. Every skill and doc says 'the owner'; this is who that is."
        )
    if PLACEHOLDER_REPO in repos:
        steps.append(
            f"Replace the `{PLACEHOLDER_REPO}` entry in agent.yaml with a repo you have, or delete it. It is an example, and `repos sync` cannot clone it."
        )
    steps.append(
        "If you use Codex: open `codex` in this directory and run /hooks to trust its hooks, now and after any update that changes .codex/hooks.json. Until then Codex skips the hooks that changed since they were last trusted, without asking."
    )
    steps.append(
        "Run `claude` (or `codex`) in this directory and say what you are working on. `/intake` turns it into a tracked feature."
    )
    lines += [f"  {i}. {step}" for i, step in enumerate(steps, 1)]
    lines.append("")
    lines.append(
        "Replies are terse by default: Claude sessions here use the `caveman-lite` output style. `/output-style default` turns it off for your own sessions."
    )
    return lines
