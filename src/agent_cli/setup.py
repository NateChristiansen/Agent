"""`agent setup`: the requirement checks, the per-OS install guides, and the configure steps.

The orchestration lives in `ops.setup` (ADR-0005: one implementation the CLI and an MCP
caller would share). This module holds what that orchestration needs and nothing else: the
checks over the tool table (`setup_tools`), `uv sync`, the caveman submodule and
its sparse checkout, and the rendering.

Every check is `shutil.which` plus, for `gh`, one `gh auth status` call. A missing tool is
installed only after one yes from the owner: `install_missing` runs each
installable tool's package-manager command, in dependency order, and whatever it cannot
install keeps its exact guide line. Nothing installs without that yes.
"""

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

# The ones without which the harness does not work: git (every record and every worktree),
# uv (the CLI itself), python 3.14 (the CLI itself), node (the hook launcher, and `npx` for any
# MCP server the owner configures), a client (claude or codex: what a session and a worker are),
# and the Obsidian app (the vault's front end; the owner's rule is that every plugin works after
# setup), and the Obsidian CLI (tab titles, state glyphs and a manager's view of its tabs all run through it).
# Everything else is optional and warns.
HARD_REQUIREMENTS = ("git", "uv", "python", "node", "claude", "codex", "obsidian app", "obsidian")
# The subset without which configuring is pointless, so setup stops before configuring. A
# missing Obsidian app does not block configuring the vault; it makes setup incomplete.
GATE_REQUIREMENTS = ("git", "uv", "python", "node", "claude", "codex")
# One of these is required, not both: a Codex-only or Claude-only user has a working harness.
CLIENTS = ("claude", "codex")


def _row(name: str, ok: bool, found: str, guide: str) -> dict[str, Any]:
    return {"name": name, "ok": ok, "found": found, "guide": guide}


def run_ok(argv: Sequence[str], cwd: Path | None = None) -> bool:
    """True when the command exits 0. Any failure to even launch it is a False, never a raise:
    a setup report that crashes because `gh` is a broken symlink helps nobody."""
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=60)
    except OSError:
        return False
    except subprocess.SubprocessError:
        return False
    return proc.returncode == 0


def python_row() -> dict[str, Any]:
    """The interpreter running this command. `setup.sh` reaches here through `uv run agent`,
    so that is the project venv's python. A system python running the CLI would have failed
    to import it long before this."""
    version = platform.python_version()
    ok = sys.version_info[:2] >= PYTHON_MIN
    minimum = ".".join(str(p) for p in PYTHON_MIN)
    return _row(
        "python",
        ok,
        f"{version} ({sys.executable})",
        "" if ok else f"uv sync installs python >= {minimum} for this project  https://docs.astral.sh/uv/",
    )


def checks(system: str | None = None) -> list[dict[str, Any]]:
    """One row per requirement: `{name, ok, found, guide}`. `found` is the resolved path (or
    the version, for python). `guide` is empty when the tool is there, and a per-OS install
    line plus the vendor URL when it is not."""
    system = system or platform.system()
    from . import installs  # imports this module, so not at the top

    # A tool winget installed earlier may be on the registry PATH but not this process's: the
    # owner's first Windows setup called an installed jq missing.
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
    """True when at least one of `CLIENTS` is on PATH, which satisfies them all."""
    return any(r["ok"] for r in rows if r["name"] in CLIENTS)


def missing_requirements(rows: list[dict[str, Any]], names: Sequence[str] = GATE_REQUIREMENTS) -> list[str]:
    """The requirements among `names` whose check rows are not satisfied. The default is the
    gate: what stops setup before it configures anything. The clients are one requirement: an
    absent one is missing only while the other is absent too."""
    either = client_found(rows)
    return [
        r["name"]
        for r in rows
        if r["name"] in names and not r["ok"] and not (either and r["name"] in CLIENTS)
    ]


def setup_step(step: str, state: str, detail: str = "") -> dict[str, Any]:
    """`state` is `done` (this run changed something), `already` (nothing to do), `skipped`
    (its client is not installed), or `failed`."""
    return {"step": step, "state": state, "detail": detail}


def harness_role(root: Path) -> dict[str, Any]:
    """A fresh install is a `consumer` (see ADR-0043); an existing `agent.local.yaml` is left as is."""
    from . import harness_role as role_mod

    state, detail = role_mod.create_local(root)
    return setup_step("harness role", state, detail)


def machine_label(root: Path, label: str | None) -> dict[str, Any]:
    """This machine's dispatch label in `agent.local.yaml`, which `dispatch new` refuses
    without. Runs after `harness_role`, which creates that file only when absent."""
    from .dispatch import machine as machine_mod

    state, detail = machine_mod.set_label(root, label or machine_mod.default_label())
    return setup_step("machine label", state, detail)


def uv_sync(root: Path) -> dict[str, Any]:
    """`uv sync`. uv prints what it changed on stderr, so an unchanged environment is
    reported as `already` rather than claiming work it did not do."""
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
    changed = any(word in noise for word in ("Installed", "Uninstalled", "Prepared", "Creating virtual"))
    return setup_step("uv sync", "done" if changed else "already", "the project venv is in sync")


def caveman_submodule(root: Path) -> dict[str, Any]:
    """Init `vendor/caveman` and apply the sparse checkout `doctor` looks for. Skipped when
    the submodule is already there and already sparse. The tell is its 38 KB CLAUDE.md: a full
    checkout has it and a sparse one does not. That file loads into every session that reads
    under the submodule, which is the whole reason for the sparse set."""
    sub = root / VENDOR_CAVEMAN
    populated = sub.exists() and any(sub.iterdir())
    if populated and not (sub / "CLAUDE.md").exists():
        return setup_step("caveman submodule", "already", "initialised and sparse")
    if not populated and not run_ok(["git", "submodule", "update", "--init", str(VENDOR_CAVEMAN)], cwd=root):
        return setup_step("caveman submodule", "failed", f"git submodule update --init {VENDOR_CAVEMAN}")
    if not run_ok(CAVEMAN_SPARSE.split(), cwd=root):
        return setup_step("caveman submodule", "failed", CAVEMAN_SPARSE)
    return setup_step("caveman submodule", "done", "initialised, sparse checkout applied")


def skill_links(root: Path) -> dict[str, Any]:
    """Make the gitignored skill links (`skill_links`): symlinks on macOS and
    Linux, junctions on Windows. Runs after the caveman submodule, which one link points into."""
    from . import skill_links as links_mod

    rows = links_mod.ensure(root)
    bad = [r for r in rows if r["state"] in ("failed", "refused")]
    if bad:
        return setup_step(
            "skill links", "failed", "; ".join(f"{r['link']}: {r.get('detail', '')}" for r in bad)
        )
    made = [r for r in rows if r["state"] in ("created", "replaced")]
    skipped = [r["link"] for r in rows if r["state"] == "skipped"]
    note = f"; skipped (target missing): {', '.join(skipped)}" if skipped else ""
    if not made:
        return setup_step("skill links", "already", f"{len(rows) - len(skipped)} link(s) in place{note}")
    return setup_step("skill links", "done", f"{len(made)} of {len(rows)} link(s) made{note}")


def git_hooks(root: Path) -> dict[str, Any]:
    """Install the pre-commit and pre-push gates (see ADR-0026). A hook someone else wrote at
    either name is kept and reported as failed, since doctor will fail on it too."""
    from . import gitgate
    from .core import AgentError

    if not (root / ".git").exists():
        return setup_step("git hooks", "already", "not a git checkout")
    try:
        result = gitgate.install(root)
    except (AgentError, OSError) as exc:
        return setup_step("git hooks", "failed", str(exc))
    detail = ", ".join(f"{name} {state}" for name, state in result.items())
    if any("foreign" in state for state in result.values()):
        return setup_step("git hooks", "failed", detail)
    changed = any(state in ("installed", "updated") for state in result.values())
    state = "done" if changed else "already"
    # agent.exportOnly gates the pre-push refusal once a notes repo is linked. A plain public
    # clone gets `false` here; a checkout with private history is left for the owner.
    export_state, export_detail = gitgate.settle_export_only(root)
    if export_state == "done":
        state = "done"
    return setup_step("git hooks", state, f"{detail}; {export_detail}")


def render(result: dict[str, Any]) -> str:
    """The report: the check table, configure steps, then doctor's own rows."""
    lines = [f"platform: {result['platform']}"]
    checks = dict_list(result["checks"])
    either = client_found(checks)

    def label(name: str) -> tuple[bool, str]:
        if name in CLIENTS:
            other = next(c for c in CLIENTS if c != name)
            return not either, f"{name} (or {other})"
        return name in HARD_REQUIREMENTS, name + ("" if name in HARD_REQUIREMENTS else " (optional)")

    rows: list[list[str]] = []
    for r in checks:
        hard, name = label(str(r["name"]))
        state = "ok" if r["ok"] else ("MISSING" if hard else "absent")
        rows.append([state, name, str(r["found"]), str(r["guide"])])
    lines.append(table(rows, ["", "requirement", "found", "install"]))
    if result.get("installed"):
        lines.append("")
        lines.append(
            table(
                [[r["state"], r["tool"], r["detail"]] for r in result["installed"]],
                ["", "install", "detail"],
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
    """Report setup success and the next missing requirements. Derive steps from actual owner identity
    and registered repos so completed setup work does not become a repeated checklist.
    """
    if result["missing"]:
        return [
            "Next: install the missing requirement(s) above, then run setup again "
            "(./setup.sh, or setup.cmd on Windows).",
            "Nothing was configured, because configuring against a missing tool reports worse "
            "errors than this list does.",
        ]
    failed = [str(c["step"]) for c in dict_list(result.get("configure")) if c.get("state") == "failed"]
    incomplete = str_list(result.get("incomplete"))
    doctor_failed = [str(c["check"]) for c in dict_list(result.get("doctor")) if c.get("level") == "fail"]
    if result.get("ok") and not (failed or incomplete or doctor_failed):
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
    if not owner:  # repos.owner_name already reads the example's placeholder as unset
        steps.append(
            "Put your name in agent.yaml: `owner: {name: ...}` is unset. "
            "Every skill and doc says 'the owner'; this is who that is."
        )
    if PLACEHOLDER_REPO in repos:
        steps.append(
            f"Replace the `{PLACEHOLDER_REPO}` entry in agent.yaml with a repo you have, or "
            "delete it. It is an example, and `repos sync` cannot clone it."
        )
    # Codex trusts hooks by exact definition and silently skips a changed one, and a reclone at the
    # same path inherits stale trust. Doctor's trust row was retired, so setup always
    # names /hooks, Codex's supported way to trust.
    steps.append(
        "If you use Codex: open `codex` in this directory and run /hooks to trust its hooks, now and "
        "after any update that changes .codex/hooks.json. Until then Codex skips the hooks that "
        "changed since they were last trusted, without asking."
    )
    steps.append(
        "Run `claude` (or `codex`) in this directory and say what you are working on. "
        "`/intake` turns it into a tracked feature."
    )
    # Numbered from the list rather than written in: with the owner already set and only the
    # example repo left, hardcoded numbers printed "2." twice and no "1.".
    lines += [f"  {i}. {step}" for i, step in enumerate(steps, 1)]
    lines.append("")
    lines.append(
        "Replies are terse by default: Claude sessions here use the `caveman-lite` output style. "
        "`/output-style default` turns it off for your own sessions."
    )
    return lines
