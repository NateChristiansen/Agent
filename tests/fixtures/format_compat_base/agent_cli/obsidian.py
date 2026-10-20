# ruff: noqa
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from . import links, obsidian_config, obsidian_run
from .core import AgentError, agent_dir_name, atomic_write, replace
from .obsidian_config import DISABLED_PLUGINS as DISABLED_PLUGINS
from .records.narrow import as_dict, as_list, str_list

OBSIDIAN_DIR = Path(".obsidian")
SNIPPET_SRC = Path("harness") / "obsidian" / "snippets"
CONFIG_SEED_SRC = Path("harness") / "obsidian" / "config"
ATTACHMENT_FOLDER = "notes/attachments"
TEMPLATE_SRC = Path("harness") / "obsidian" / "templates"
SNIPPET_NAMES = ("agent-callouts", "agent-terminal-tabs")
LOCAL_PLUGINS = ("agent-terminal-focus", "agent-hidden-files")
LOCAL_PLUGIN_SRC = Path("harness") / "obsidian" / "plugins"
JOURNAL_DIR = "journal"
RECOMMENDED_PLUGINS = {
    "terminal": "Terminal (polyipseity) — claude sessions inside the vault",
    "obsidian-shellcommands": "Shell commands — bin/agent actions in the command palette",
    "cmdr": "Commander — those actions on the ribbon",
    "homepage": "Homepage — open Dashboard.md on launch",
    "obsidian-hover-editor": "Hover Editor — peek at logs from the dashboard",
    "recent-files-obsidian": "Recent Files — navigation across feature folders",
    "obsidian-git": "Git — history and diff views (keep auto-commit off)",
    "callout-manager": "Callout Manager — tweak the log-entry callout styles",
    "vscode-editor": "VSCode Editor — open json/yaml/py/jsonl in a Monaco tab inside Obsidian",
    "dataview": "Dataview — the Dashboard's live views (harness/obsidian/views), JavaScript queries on",
}
OPTIONAL_PLUGINS = {
    "agent-client": "Agent Client — ACP chat pane; verify hooks fire before relying on it",
    "templater-obsidian": "Templater — only with 'trigger on new file' OFF",
}
PLUGIN_SOURCES: dict[str, tuple[str, str]] = {
    "callout-manager": ("eth-p/obsidian-callout-manager", "1.1.2"),
    "cmdr": ("jsmorabito/obsidian-commander", "0.5.10"),
    "dataview": ("blacksmithgu/obsidian-dataview", "0.5.68"),
    "homepage": ("mirnovov/obsidian-homepage", "4.5.0"),
    "obsidian-git": ("vinzent03/obsidian-git", "2.39.0"),
    "obsidian-hover-editor": ("nothingislost/obsidian-hover-editor", "0.11.31"),
    "obsidian-shellcommands": ("taitava/obsidian-shellcommands", "0.23.0"),
    "recent-files-obsidian": ("tgrosinger/recent-files-obsidian", "1.7.10"),
    "show-hidden-files": ("witi42/obsidian-show-hidden-files", "1.0.3"),
    "terminal": ("polyipseity/obsidian-terminal", "3.27.2"),
    "vscode-editor": ("sunxvming/obsidian-vscode-editor", "1.0.5"),
}
PLUGIN_FILES = ("main.js", "manifest.json", "styles.css")
PLUGIN_OPTIONAL_FILES = ("styles.css",)
DATAVIEW_DATA = Path("plugins") / "dataview" / "data.json"
DATAVIEW_SETTINGS = {"enableDataviewJs": True, "enableInlineDataviewJs": False}


def installed_version(odir: Path, pid: str) -> str | None:
    manifest = odir / "plugins" / pid / "manifest.json"
    if not manifest.exists():
        return None
    try:
        return str(json.loads(manifest.read_text(encoding="utf-8")).get("version") or "") or None
    except OSError, ValueError:
        return None


def _fetch(url: str, timeout: float = 30.0) -> bytes | None:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return bytes(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def install_plugins(root: Path, ids: list[str] | None = None) -> list[dict[str, Any]]:
    odir = obsidian_dir(root)
    rows: list[dict[str, Any]] = []
    if os.environ.get("AGENT_NOW") or os.environ.get("AGENT_NO_PLUGIN_FETCH"):
        return [{"plugin": pid, "state": "skipped", "detail": "fetch disabled"} for pid in PLUGIN_SOURCES]
    for pid in ids if ids is not None else PLUGIN_SOURCES:
        repo, version = PLUGIN_SOURCES[pid]
        current = installed_version(odir, pid)
        if current == version:
            rows.append({"plugin": pid, "state": "already", "version": version})
            continue
        target = odir / "plugins" / pid
        try:
            payloads: dict[str, bytes] = {}
            for name in PLUGIN_FILES:
                data = _fetch(f"https://github.com/{repo}/releases/download/{version}/{name}")
                if data is None:
                    if name in PLUGIN_OPTIONAL_FILES:
                        continue
                    raise AgentError(f"{repo} {version} has no {name}")
                payloads[name] = data
        except (AgentError, OSError, ValueError) as exc:
            rows.append({"plugin": pid, "state": "failed", "version": version, "detail": str(exc)})
            continue
        target.mkdir(parents=True, exist_ok=True)
        for name, data in payloads.items():
            atomic_write(target / name, data.decode("utf-8"))
        rows.append({"plugin": pid, "state": "updated" if current else "installed", "version": version})
    return rows


def obsidian_dir(root: Path) -> Path:
    return root / OBSIDIAN_DIR


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AgentError(f"{path} is not valid JSON: {exc}") from exc
    return as_dict(data)


def _read_list(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AgentError(f"{path} is not valid JSON: {exc}") from exc
    return [str(x) for x in as_list(data)]


def write_json(path: Path, data: Any) -> None:
    atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False))


def _copy_if_changed(src: Path, dst: Path) -> None:
    content = src.read_bytes()
    if dst.exists() and dst.read_bytes() == content:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{dst.name}.", dir=dst.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
        os.chmod(tmp, 420)
        replace(tmp, dst)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _vault_dir(path: Path, parents: bool = False) -> bool:
    if links.is_link(path) and (not path.exists()):
        return False
    path.mkdir(parents=parents, exist_ok=True)
    return True


def setup(root: Path) -> dict[str, Any]:
    odir = obsidian_dir(root)
    odir.mkdir(exist_ok=True)
    written: list[str] = []
    seeds = root / CONFIG_SEED_SRC
    if seeds.is_dir():
        for src in sorted((p for p in seeds.rglob("*") if p.is_file())):
            dst = odir / src.relative_to(seeds)
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(dst, src.read_text(encoding="utf-8"))
                written.append(dst.relative_to(root).as_posix())
    for snippet_name in SNIPPET_NAMES:
        src = root / SNIPPET_SRC / f"{snippet_name}.css"
        if src.exists():
            dst = odir / "snippets" / f"{snippet_name}.css"
            _copy_if_changed(src, dst)
            written.append(dst.relative_to(root).as_posix())
    appearance = read_json(odir / "appearance.json")
    snippets = str_list(appearance.get("enabledCssSnippets"))
    for snippet_name in SNIPPET_NAMES:
        if snippet_name not in snippets:
            snippets.append(snippet_name)
    appearance["enabledCssSnippets"] = snippets
    write_json(odir / "appearance.json", appearance)
    written.append(".obsidian/appearance.json")
    app = read_json(odir / "app.json")
    app["alwaysUpdateLinks"] = False
    app["useMarkdownLinks"] = False
    app["attachmentFolderPath"] = ATTACHMENT_FOLDER
    app["newFileLocation"] = "folder"
    app["newFileFolderPath"] = "notes"
    app["showUnsupportedFiles"] = True
    ignore = str_list(app.get("userIgnoreFilters"))
    for pattern in (
        ".git/",
        ".venv/",
        f"{agent_dir_name()}/",
        "repos/",
        ".pytest_cache/",
        ".ruff_cache/",
        "src/",
        "tests/",
        "vendor/",
    ):
        if pattern not in ignore:
            ignore.append(pattern)
    app["userIgnoreFilters"] = ignore
    write_json(odir / "app.json", app)
    written.append(".obsidian/app.json")
    for folder in ("notes",):
        if not _vault_dir(root / folder, parents=True):
            continue
        keep = root / folder / ".gitkeep"
        if not keep.exists():
            keep.write_text("")
    (root / TEMPLATE_SRC).mkdir(parents=True, exist_ok=True)
    daily = read_json(odir / "daily-notes.json")
    daily.update(
        {
            "folder": JOURNAL_DIR,
            "format": "YYYY/MM-MMMM/DD-dddd",
            "template": (TEMPLATE_SRC / "daily").as_posix(),
            "autorun": False,
        }
    )
    write_json(odir / "daily-notes.json", daily)
    written.append(".obsidian/daily-notes.json")
    _vault_dir(root / JOURNAL_DIR)
    templates = read_json(odir / "templates.json")
    templates["folder"] = TEMPLATE_SRC.as_posix()
    write_json(odir / "templates.json", templates)
    written.append(".obsidian/templates.json")
    core = read_json(odir / "core-plugins.json")
    for cid in (
        "daily-notes",
        "templates",
        "bases",
        "properties",
        "backlink",
        "graph",
        "outgoing-link",
        "command-palette",
        "page-preview",
        "bookmarks",
        "outline",
    ):
        core[cid] = True
    write_json(odir / "core-plugins.json", core)
    written.append(".obsidian/core-plugins.json")
    enabled = [pid for pid in _read_list(odir / "community-plugins.json") if pid not in DISABLED_PLUGINS]
    for pid in (*RECOMMENDED_PLUGINS, *LOCAL_PLUGINS):
        if pid not in enabled:
            enabled.append(pid)
    write_json(odir / "community-plugins.json", enabled)
    written.append(".obsidian/community-plugins.json")
    dataview = read_json(odir / DATAVIEW_DATA)
    if (
        any((dataview.get(k) != v for k, v in DATAVIEW_SETTINGS.items()))
        or not (odir / DATAVIEW_DATA).exists()
    ):
        dataview.update(DATAVIEW_SETTINGS)
        (odir / DATAVIEW_DATA).parent.mkdir(parents=True, exist_ok=True)
        write_json(odir / DATAVIEW_DATA, dataview)
    written.append((OBSIDIAN_DIR / DATAVIEW_DATA).as_posix())
    written.extend(obsidian_config.enforce(root, odir))
    for pid in LOCAL_PLUGINS:
        for name in ("manifest.json", "main.js"):
            src = root / LOCAL_PLUGIN_SRC / pid / name
            if not src.is_file():
                continue
            dst = odir / "plugins" / pid / name
            _copy_if_changed(src, dst)
            written.append(dst.relative_to(root).as_posix())
    plugins = install_plugins(root)
    installed = (
        {p.name for p in (odir / "plugins").iterdir() if (p / "manifest.json").exists()}
        if (odir / "plugins").exists()
        else set[str]()
    )
    missing = [f"{pid}: {desc}" for pid, desc in RECOMMENDED_PLUGINS.items() if pid not in installed]
    return {
        "written": written,
        "plugins": plugins,
        "missing_plugins": missing,
        "optional_plugins": [f"{k}: {v}" for k, v in OPTIONAL_PLUGINS.items()],
        "cli_available": cli_path() is not None,
    }


CLI_FALLBACKS = ("/usr/local/bin/obsidian", "/opt/homebrew/bin/obsidian")


def cli_path() -> str | None:
    env = os.environ.get("OBSIDIAN_CLI")
    if env and Path(env).is_file():
        return env
    return shutil.which("obsidian") or next((p for p in CLI_FALLBACKS if Path(p).is_file()), None)


APP_QUERY_TIMEOUT_S = 5


def cli_version(exe: str, timeout: float = 10) -> tuple[bool | None, str]:
    try:
        proc = obsidian_run.run([exe, "version"], timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return (False, f"`obsidian version` failed: {exc}")
    if proc is None:
        return (None, "unverified: Obsidian is not running")
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 or not out:
        detail = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip().splitlines()
        return (False, f"`obsidian version` failed: {(detail[-1] if detail else proc.returncode)}")
    return (True, out.splitlines()[0])


def cli_row(name: str, guide: str) -> dict[str, Any]:
    found = cli_path()
    if not found:
        return {"name": name, "ok": False, "found": "", "guide": guide, "verified": False}
    works, detail = cli_version(found)
    return {
        "name": name,
        "ok": works is not False,
        "found": f"{found} ({detail})",
        "guide": guide if works is False else "",
        "verified": works is True,
    }


def app_running() -> bool:
    if sys.platform == "win32":
        argv = ["tasklist", "/FI", "IMAGENAME eq Obsidian.exe", "/NH", "/FO", "CSV"]
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, check=False, timeout=APP_QUERY_TIMEOUT_S
            )
        except OSError, subprocess.TimeoutExpired:
            return False
        return proc.returncode == 0 and "obsidian.exe" in proc.stdout.lower()
    try:
        proc = subprocess.run(
            ["ps", "-axo", "comm="], capture_output=True, text=True, check=False, timeout=APP_QUERY_TIMEOUT_S
        )
    except OSError, subprocess.TimeoutExpired:
        return False
    return proc.returncode == 0 and any(
        (Path(line.strip()).name in ("Obsidian", "obsidian") for line in proc.stdout.splitlines())
    )


def run_cli(args: list[str], require_running: bool = True) -> dict[str, Any]:
    exe = cli_path()
    if exe is None:
        return {
            "ok": False,
            "skipped": "obsidian CLI not on PATH (enable it in Settings → General → Command line interface)",
        }
    if not require_running and (not obsidian_run.launch()):
        return {"ok": False, "skipped": "Obsidian did not start"}
    try:
        proc = obsidian_run.run([exe, *args], 20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "skipped": f"obsidian CLI failed: {exc}"}
    if proc is None:
        return {"ok": False, "skipped": "Obsidian is not running"}
    return {
        "ok": proc.returncode == 0,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
        "code": proc.returncode,
    }


def open_note(root: Path, rel_path: str, require_running: bool = False) -> dict[str, Any]:
    target = rel_path[:-3] if rel_path.endswith(".md") else rel_path
    return run_cli(["open", f"file={target}"], require_running=require_running)


def auto_open(root: Path, rel_path: str) -> None:
    if os.environ.get("AGENT_NO_AUTOOPEN"):
        return
    open_note(root, rel_path, require_running=True)


def daily() -> dict[str, Any]:
    return run_cli(["daily"], require_running=False)


def search(query: str) -> dict[str, Any]:
    return run_cli(["search", f"query={query}"], require_running=False)
