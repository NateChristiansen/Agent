const { addIcon, Modal, Notice, Plugin, MarkdownRenderChild } = require('obsidian');
const { execFile } = require('child_process');
const { randomUUID } = require('crypto');
const path = require('path');

const CODEX_STARTING_TITLE = '✅ Codex manager';
const CLAUDE_STARTING_TITLE = '✅ Claude manager';
const PROVISIONAL_MS = 30000;
// Monochrome brand marks sit in Obsidian's 100 × 100 custom icon box and inherit its theme color.
// OpenAI: https://openai.com/brand/ (OpenAI-Logos-2025.zip, black monoblossom).
// Claude: https://github.com/simple-icons/simple-icons/blob/develop/icons/claude.svg.
const OPENAI_ICON = '<g fill="currentColor" transform="translate(-12.45 -12.75) scale(0.1732)"><path d="M304.246 294.611V249.028C304.246 245.189 305.687 242.309 309.044 240.392L400.692 187.612C413.167 180.415 428.042 177.058 443.394 177.058C500.971 177.058 537.44 221.682 537.44 269.182C537.44 272.54 537.44 276.379 536.959 280.218L441.954 224.558C436.197 221.201 430.437 221.201 424.68 224.558L304.246 294.611ZM518.245 472.145V363.224C518.245 356.505 515.364 351.707 509.608 348.349L389.174 278.296L428.519 255.743C431.877 253.826 434.757 253.826 438.115 255.743L529.762 308.523C556.154 323.879 573.905 356.505 573.905 388.171C573.905 424.636 552.315 458.225 518.245 472.141V472.145ZM275.937 376.182L236.592 353.152C233.235 351.235 231.794 348.354 231.794 344.515V238.956C231.794 187.617 271.139 148.749 324.4 148.749C344.555 148.749 363.264 155.468 379.102 167.463L284.578 222.164C278.822 225.521 275.942 230.319 275.942 237.039V376.186L275.937 376.182ZM360.626 425.122L304.246 393.455V326.283L360.626 294.616L417.002 326.283V393.455L360.626 425.122ZM396.852 570.989C376.698 570.989 357.989 564.27 342.151 552.276L436.674 497.574C442.431 494.217 445.311 489.419 445.311 482.699V343.552L485.138 366.582C488.495 368.499 489.936 371.379 489.936 375.219V480.778C489.936 532.117 450.109 570.985 396.852 570.985V570.989ZM283.134 463.99L191.486 411.211C165.094 395.854 147.343 363.229 147.343 331.562C147.343 294.616 169.415 261.509 203.48 247.593V356.991C203.48 363.71 206.361 368.508 212.117 371.866L332.074 441.437L292.729 463.99C289.372 465.907 286.491 465.907 283.134 463.99ZM277.859 542.68C223.639 542.68 183.813 501.895 183.813 451.514C183.813 447.675 184.294 443.836 184.771 439.997L279.295 494.698C285.051 498.056 290.812 498.056 296.568 494.698L417.002 425.127V470.71C417.002 474.549 415.562 477.429 412.204 479.346L320.557 532.126C308.081 539.323 293.206 542.68 277.854 542.68H277.859ZM396.852 599.776C454.911 599.776 503.37 558.513 514.41 503.812C568.149 489.896 602.696 439.515 602.696 388.176C602.696 354.587 588.303 321.962 562.392 298.45C564.791 288.373 566.231 278.296 566.231 268.224C566.231 199.611 510.571 148.267 446.274 148.267C433.322 148.267 420.846 150.184 408.37 154.505C386.775 133.392 357.026 119.958 324.4 119.958C266.342 119.958 217.883 161.22 206.843 215.921C153.104 229.837 118.557 280.218 118.557 331.557C118.557 365.146 132.95 397.771 158.861 421.283C156.462 431.36 155.022 441.437 155.022 451.51C155.022 520.123 210.682 571.466 274.978 571.466C287.931 571.466 300.407 569.549 312.883 565.228C334.473 586.341 364.222 599.776 396.852 599.776Z"/></g>';
const CLAUDE_ICON = '<g fill="currentColor" transform="translate(8 8) scale(3.5)"><path d="m4.7144 15.9555 4.7174-2.6471.079-.2307-.079-.1275h-.2307l-.7893-.0486-2.6956-.0729-2.3375-.0971-2.2646-.1214-.5707-.1215-.5343-.7042.0546-.3522.4797-.3218.686.0608 1.5179.1032 2.2767.1578 1.6514.0972 2.4468.255h.3886l.0546-.1579-.1336-.0971-.1032-.0972L6.973 9.8356l-2.55-1.6879-1.3356-.9714-.7225-.4918-.3643-.4614-.1578-1.0078.6557-.7225.8803.0607.2246.0607.8925.686 1.9064 1.4754 2.4893 1.8336.3643.3035.1457-.1032.0182-.0728-.164-.2733-1.3539-2.4467-1.445-2.4893-.6435-1.032-.17-.6194c-.0607-.255-.1032-.4674-.1032-.7285L6.287.1335 6.6997 0l.9957.1336.419.3642.6192 1.4147 1.0018 2.2282 1.5543 3.0296.4553.8985.2429.8318.091.255h.1579v-.1457l.1275-1.706.2368-2.0947.2307-2.6957.0789-.7589.3764-.9107.7468-.4918.5828.2793.4797.686-.0668.4433-.2853 1.8517-.5586 2.9021-.3643 1.9429h.2125l.2429-.2429.9835-1.3053 1.6514-2.0643.7286-.8196.85-.9046.5464-.4311h1.0321l.759 1.1293-.34 1.1657-1.0625 1.3478-.8804 1.1414-1.2628 1.7-.7893 1.36.0729.1093.1882-.0183 2.8535-.607 1.5421-.2794 1.8396-.3157.8318.3886.091.3946-.3278.8075-1.967.4857-2.3072.4614-3.4364.8136-.0425.0304.0486.0607 1.5482.1457.6618.0364h1.621l3.0175.2247.7892.522.4736.6376-.079.4857-1.2142.6193-1.6393-.3886-3.825-.9107-1.3113-.3279h-.1822v.1093l1.0929 1.0686 2.0035 1.8092 2.5075 2.3314.1275.5768-.3218.4554-.34-.0486-2.2039-1.6575-.85-.7468-1.9246-1.621h-.1275v.17l.4432.6496 2.3436 3.5214.1214 1.0807-.17.3521-.6071.2125-.6679-.1214-1.3721-1.9246L14.38 17.959l-1.1414-1.9428-.1397.079-.674 7.2552-.3156.3703-.7286.2793-.6071-.4614-.3218-.7468.3218-1.4753.3886-1.9246.3157-1.53.2853-1.9004.17-.6314-.0121-.0425-.1397.0182-1.4328 1.9672-2.1796 2.9446-1.7243 1.8456-.4128.164-.7164-.3704.0667-.6618.4008-.5889 2.386-3.0357 1.4389-1.882.929-1.0868-.0062-.1579h-.0546l-6.3385 4.1164-1.1293.1457-.4857-.4554.0608-.7467.2307-.2429 1.9064-1.3114Z"/></g>';

// Windows: PowerShell through conhost, whose window the terminal plugin's Python
// helper manages with pywinctl from the private venv setup makes (installs.terminal_python).
function windowsManagerProfile(client, launchMarker, vaultPath) {
  return {
    type: 'integrated',
    executable: 'powershell.exe',
    args: ['-NoLogo', '-NoExit', '-Command', `$env:AGENT_RIBBON_LAUNCH='${launchMarker}'; ${client}`],
    environment: [],
    followTheme: true,
    name: '',
    platforms: { win32: true },
    pythonExecutable: `${vaultPath}\\.agent\\terminal-python\\Scripts\\python.exe`,
    restoreHistory: false,
    rightClickAction: 'copyPaste',
    successExitCodes: ['0', 'SIGINT', 'SIGTERM'],
    terminalOptions: { documentOverride: null },
    useWin32Conhost: true,
  };
}

// xterm's default OSC 8 handler confirms, then calls window.open() with no URL and sets
// location afterwards; Obsidian blocks the blank window, so a clicked hyperlink (Codex's
// sign-in link) did nothing. HTTP(S) and same-vault Obsidian links use the external opener;
// in-vault file URIs use Obsidian's own workspace. All other destinations are dropped.
function linkHandler(vaultName, app) {
  let linkedEntries = [];
  const relativeTo = (root, file) => {
    const relative = path.relative(root, file);
    return relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative) ? null : relative;
  };
  function fileLink(root, file) {
    let realRoot, realFile;
    try {
      realRoot = require('fs').realpathSync(root);
      realFile = require('fs').realpathSync(file);
    } catch {
      return relativeTo(root, file);
    }
    const relative = relativeTo(realRoot, realFile);
    if (relative !== null) return relative;
    const mappedLink = () => {
      for (const [entry, target] of linkedEntries) {
        const rest = relativeTo(target, realFile);
        if (rest !== null) return path.join(entry, rest);
      }
      return null;
    };
    const cached = mappedLink();
    if (cached !== null) return cached;
    // Private vault entries point into the sibling notes repo. Refresh on a miss.
    linkedEntries = [];
    for (const entry of require('fs').readdirSync(root, {withFileTypes: true})) {
      if (!entry.isSymbolicLink()) continue;
      try {
        linkedEntries.push([entry.name, require('fs').realpathSync(path.join(root, entry.name))]);
      } catch {} // A dangling entry must not hide the other links.
    }
    return mappedLink();
  }
  return {
  allowNonHttpProtocols: true,
  activate(event, uri) {
    let url;
    try {
      url = new URL(uri);
    } catch {
      return;
    }
    if (url.protocol === 'file:') {
      if (url.host && url.host !== 'localhost') return;
      try {
        const root = path.resolve(app.vault.adapter.getBasePath());
        const file = path.resolve(require('url').fileURLToPath(url));
        if (process.platform === 'win32' && [root, file].some((p) => p.startsWith('\\\\') || p.startsWith('//'))) return;
        const relative = fileLink(root, file);
        if (!relative) return;
        const newLeaf = !!(event?.metaKey || event?.ctrlKey);
        const f = app.vault.getAbstractFileByPath(relative.split(path.sep).join('/'));
        if (f) app.workspace.getLeaf(newLeaf).openFile(f);
      } catch {}
      return;
    }
    if (url.protocol !== 'http:' && url.protocol !== 'https:') {
      if (url.protocol !== 'obsidian:' || url.host !== 'open' || !['', '/'].includes(url.pathname)
        || url.username || url.password || url.hash || !vaultName
        || url.searchParams.getAll('vault').length !== 1 || url.searchParams.get('vault') !== vaultName
        || url.searchParams.getAll('file').length !== 1 || !url.searchParams.get('file')?.trim()
        || [...url.searchParams.keys()].some((key) => !['vault', 'file'].includes(key))) return;
    }
    try {
      require('electron').shell.openExternal(url.href);
    } catch {
      window.open(url.href);
    }
  },
}; }

function patchLinkHandler(leaf, handler) {
  const terminal = leaf?.view?.emulator?.terminal;
  if (!terminal || terminal.options.linkHandler === handler) return;
  terminal.options.linkHandler = handler;
}

function editorFind(workspace, event) {
  if (process.platform !== 'darwin' || !event.metaKey || event.ctrlKey || event.altKey || event.shiftKey
    || event.key?.toLowerCase() !== 'f' || event.defaultPrevented) return;
  const view = workspace.activeLeaf?.view;
  if (view?.getViewType() !== 'vscode-editor' || !view.monacoEditor?.hasTextFocus()) return;
  view.monacoEditor.trigger('agent-terminal-focus', 'actions.find', null);
  event.preventDefault();
  event.stopImmediatePropagation();
}

// Cache parsed snapshots, but still stat on each tick so missed file events recover by polling.
class JsonSnapshot {
  constructor(file, fsImpl = require('fs')) { this.file = file; this.fs = fsImpl.promises; }
  async read() {
    try {
      const stat = await this.fs.stat(this.file);
      if (this.stat && this.stat.mtimeMs === stat.mtimeMs && this.stat.size === stat.size
        && this.stat.ino === stat.ino) return this.value;
      const value = JSON.parse(await this.fs.readFile(this.file, 'utf8'));
      this.value = value;
      this.stat = stat;
      return value;
    } catch (error) {
      this.stat = undefined;
      throw error;
    }
  }
}

function managerProfile(client, launchMarker, vaultPath) {
  if (process.platform === 'win32') return windowsManagerProfile(client, launchMarker, vaultPath);
  const shell = process.platform === 'darwin' ? '/bin/zsh' : '/bin/bash';
  const login = process.platform === 'darwin' ? '--login' : '-l';
  return {
    type: 'integrated',
    executable: shell,
    args: ['-i', login, '-c', `AGENT_RIBBON_LAUNCH=${launchMarker} ${client}; exec ${shell} ${login}`],
    environment: [],
    followTheme: true,
    name: '',
    platforms: { darwin: process.platform === 'darwin', linux: process.platform === 'linux' },
    pythonExecutable: 'python3',
    restoreHistory: false,
    rightClickAction: 'copyPaste',
    successExitCodes: ['0', 'SIGINT', 'SIGTERM'],
    terminalOptions: { documentOverride: null },
    useWin32Conhost: true,
  };
}

// The Dashboard's "Clear leases" link: the owner frees a stuck session's
// leases in one click, asked to confirm only when the session was active in the last 30 minutes.
// The venv's own entry point runs with no shell, so the session id is never parsed by one. On
// Windows that is the venv's python, not agent.exe, which `uv sync` cannot replace while any
// process runs it.
function agentCommand(vaultPath) {
  return process.platform === 'win32'
    ? path.join(vaultPath, '.venv', 'Scripts', 'python.exe')
    : path.join(vaultPath, '.venv', 'bin', 'agent');
}

function leaseClearArgs(vaultPath, session, confirm) {
  const module = process.platform === 'win32' ? ['-I', '-m', 'agent_cli'] : [];
  const args = [...module, '--root', vaultPath, '--json', 'lease', 'clear', session];
  return confirm ? [...args, '--yes'] : args;
}

function runLeaseClear(vaultPath, session, confirm) {
  return new Promise((resolve) => {
    execFile(agentCommand(vaultPath), leaseClearArgs(vaultPath, session, confirm), { cwd: vaultPath, timeout: 30000 },
      (error, stdout, stderr) => {
        let data = null;
        try {
          data = JSON.parse(stdout);
        } catch {
          data = null;
        }
        // `--json` reports an AgentError as {error, code} on stdout with a nonzero exit.
        if (error || !data || data.error || !Array.isArray(data.cleared)) {
          resolve({ error: String(data?.error || stderr || error?.message || 'lease clear failed').trim() });
          return;
        }
        resolve({ data });
      });
  });
}

// Windows: closing a manager tab kills only the terminal plugin's direct
// child, the session conhost. cmd, the PowerShell our profile starts, and the Claude or Codex
// under it kept running. The plugin keeps that child in private fields,
// so no tab-close event is trusted here: a sweep finds PowerShell chains whose conhost is
// missing from two process snapshots in a row and ends each one. A live tab's conhost was
// present in every native snapshot, and one snapshot that omits it is not enough to act on.
// The whole CommandLine, as Windows reported it for windowsManagerProfile's args after the
// terminal plugin's .bat quoting (read natively), and the cmd that runs that .bat.
const LAUNCH_RE = /^"powershell\.exe" {1,2}"-NoLogo" "-NoExit" "-Command" "\$env:AGENT_RIBBON_LAUNCH='(claude|codex):([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})'; (claude|codex)" ?$/;
// A Workers-row tab (`dispatch open`, opener._windows_profile) runs the same chain with a
// worker marker and one of two commands, so closing it leaves `watch --follow` holding the venv
// python. Its shape is the manager one's, which Windows reported with every character
// this command uses but `.` and `/`; not yet read natively.
const WORKER_LAUNCH_RE = /^"powershell\.exe" {1,2}"-NoLogo" "-NoExit" "-Command" "\$env:AGENT_WORKER_LAUNCH='worker:([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})'; (?:\.\/bin\/agent\.cmd dispatch watch '[A-Za-z0-9._-]{1,128}' --follow|claude --resume '[A-Za-z0-9._-]{1,128}')" ?$/;
const BAT_RE = /^[A-Za-z]:\\[^"&|<>^]*\\cmd\.exe \/c [A-Za-z]:\\[^"&|<>^]*\\tmp-\d+-[A-Za-z0-9_-]+\.bat$/i;
// Lists processes only; no marker, pid or path is ever passed in, so the query cannot match
// itself, and its own pid is excluded as well.
const PROCESS_QUERY = '$ErrorActionPreference = "Stop"; '
  + 'ConvertTo-Json -Compress -InputObject @(Get-CimInstance Win32_Process | ForEach-Object { '
  + '[pscustomobject]@{ p = $_.ProcessId; pp = $_.ParentProcessId; n = $_.Name; '
  + 't = $(if ($_.CreationDate) { $_.CreationDate.ToFileTimeUtc() } else { 0 }); '
  + 'c = $(if ($_.Name -in @("powershell.exe", "cmd.exe")) { $_.CommandLine } else { $null }) } })';

// Manager or worker PowerShells to end, as {p, t, pp, ct}: one whose cmd parent outlived its
// conhost. Exactly one marked PowerShell per cmd, each launch marker once, and known creation
// times, or the chain is left alone.
function orphanChains(processes, selfPid) {
  const byPid = new Map(processes.map((p) => [p.p, p]));
  const shells = [];
  for (const proc of processes) {
    if (proc.p === selfPid || String(proc.n).toLowerCase() !== 'powershell.exe') continue;
    const match = LAUNCH_RE.exec(proc.c || '');
    if (match && match[1] === match[3]) {
      shells.push({ proc, marker: `${match[1]}:${match[2]}` });
      continue;
    }
    const worker = WORKER_LAUNCH_RE.exec(proc.c || '');
    if (worker) shells.push({ proc, marker: `worker:${worker[1]}` });
  }
  const perCmd = new Map();
  const perMarker = new Map();
  for (const { proc, marker } of shells) {
    perCmd.set(proc.pp, (perCmd.get(proc.pp) || 0) + 1);
    perMarker.set(marker, (perMarker.get(marker) || 0) + 1);
  }
  const out = [];
  for (const { proc, marker } of shells) {
    if (perCmd.get(proc.pp) !== 1 || perMarker.get(marker) !== 1) continue;
    const cmd = byPid.get(proc.pp);
    if (!cmd || String(cmd.n).toLowerCase() !== 'cmd.exe' || !BAT_RE.test(cmd.c || '')) continue;
    if (!(proc.t > 0) || !(cmd.t > 0) || cmd.t > proc.t) continue;
    const host = byPid.get(cmd.pp);
    // gone, or the pid now belongs to a process started after the cmd (pid reuse)
    if (!host || (host.t > 0 && host.t > cmd.t)) out.push({ p: proc.p, t: proc.t, pp: proc.pp, ct: cmd.t });
  }
  return out;
}

async function snapshot() {
  const query = await runFile('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', PROCESS_QUERY]);
  if (query.error) throw new Error(`process query failed: ${String(query.stderr || query.error.message).trim()}`);
  const processes = JSON.parse(query.stdout);
  if (!Array.isArray(processes)) throw new Error('process query returned no list');
  return orphanChains(processes, query.pid);
}

function runFile(cmd, args) {
  return new Promise((resolve) => {
    const child = execFile(cmd, args, { timeout: 15000, windowsHide: true, maxBuffer: 16 * 1024 * 1024 },
      (error, stdout, stderr) => resolve({ error, stdout, stderr, pid: child?.pid }));
  });
}

// Two snapshots: a chain is ended only when the second, taken just before the kill, shows the
// same PowerShell (pid and birth time) under the same cmd, still orphaned. One snapshot with a
// missing parent is not proof it died (Codex review).
async function reapOrphans() {
  const first = await snapshot();
  if (!first.length) return [];
  const second = await snapshot();
  const same = (a, b) => a.p === b.p && a.t === b.t && a.pp === b.pp && a.ct === b.ct;
  const reaped = [];
  for (const pid of first.filter((a) => second.some((b) => same(a, b))).map((a) => a.p)) {
    const kill = await runFile('taskkill.exe', ['/PID', String(pid), '/T', '/F']);
    if (kill.error) throw new Error(`taskkill ${pid} failed: ${String(kill.stderr || kill.error.message).trim()}`);
    reaped.push(pid);
  }
  return reaped;
}

// A tab the ribbon opened: its saved profile is exactly what managerProfile or
// windowsManagerProfile wrote, with the same client in the marker and the command.
const UUID_V4 = '[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}';
const WINDOWS_COMMAND_RE = new RegExp(`^\\$env:AGENT_RIBBON_LAUNCH='(claude|codex):${UUID_V4}'; (claude|codex)$`);
const UNIX_COMMAND_RE = new RegExp(
  `^AGENT_RIBBON_LAUNCH=(claude|codex):${UUID_V4} (claude|codex); exec (/bin/zsh --login|/bin/bash -l)$`);
const UNIX_SHELLS = { '/bin/zsh': '--login', '/bin/bash': '-l' };

// The client ('claude' or 'codex') of a ribbon manager profile, or null for any other profile.
function managerClient(profile) {
  const args = profile?.args;
  if (!Array.isArray(args) || args.length !== 4) return null;
  if (profile.executable === 'powershell.exe') {
    const match = WINDOWS_COMMAND_RE.exec(String(args[3]));
    const ok = args[0] === '-NoLogo' && args[1] === '-NoExit' && args[2] === '-Command'
      && !!match && match[1] === match[2];
    return ok ? match[1] : null;
  }
  const login = UNIX_SHELLS[profile.executable];
  const match = UNIX_COMMAND_RE.exec(String(args[3]));
  const ok = !!login && args[0] === '-i' && args[1] === login && args[2] === '-c'
    && !!match && match[1] === match[2] && match[3] === `${profile.executable} ${login}`;
  return ok ? match[1] : null;
}

function isManagerProfile(profile) {
  return managerClient(profile) !== null;
}

function leafClient(leaf) {
  let state;
  try {
    state = leaf.getViewState();
  } catch {
    return null;
  }
  if (state?.type !== 'terminal:terminal') return null;
  return managerClient(state.state?.['terminal:terminal']?.profile);
}

function isManagerLeaf(leaf) {
  return leafClient(leaf) !== null;
}

// Windows: Codex's TUI pastes the clipboard on the right mouse button itself,
// and the Terminal plugin's copyPaste action pasted it a second time; a multi-paragraph paste
// even submitted between the two. In a ribbon Codex tab only, a right-click with no selection
// stops before the plugin's handler, so Codex's own paste is the one paste. A selection still
// reaches the plugin, which copies it. Claude tabs and every other terminal are untouched.
// `guarded` is the plugin instance's own set, and `register` its disposer list, so an unload or
// reload removes every listener and a fresh load starts clean (Codex review).
function guardCodexRightClick(leaf, guarded, register) {
  if (process.platform !== 'win32' || leafClient(leaf) !== 'codex') return false;
  const view = leaf.view;
  const el = view?.containerEl;
  if (!el || guarded.has(el)) return false;
  guarded.add(el);
  const onContextMenu = (event) => {
    // only a right-click on the terminal surface; the view's other menus are left alone
    if (!event.target?.closest?.('.xterm')) return;
    if (view.emulator?.terminal?.hasSelection?.()) return;
    event.preventDefault();
    event.stopPropagation();
  };
  el.addEventListener('contextmenu', onContextMenu, { capture: true });
  register(() => {
    el.removeEventListener('contextmenu', onContextMenu, { capture: true });
    guarded.delete(el);
  });
  return true;
}

// Tab painting. `.agent/live.json` carries a `tabs` list that Python
// builds with the same code the hooks' relabel used (`vault/live.py` tab_entries): per tab, the
// ready `label`, the `stale_label` to show once `valid_until` has passed, and how to find the tab.
// This only matches a tab and copies a string; no labelling rule lives here (ADR-0015). The
// match is the drainer's (`tab_drain._batch_code`): the exact name in the title the tab carries
// or, for a generated name, shows; failing that, the session's `· cl 834e` suffix; and a name
// that finds two tabs paints neither.
const LIVE_JSON = path.join('.agent', 'live.json');
const TABS_OFF = path.join('.agent', 'tabs-off');
// One file per failed snapshot write (`live.WRITE_FAILED`); any standing means the snapshot is stale.
const LIVE_FAILED = path.join('.agent', 'live-failed');
// A write in flight records an `intent-` file there first (`live.begin_intent`).
// One younger than this is painted through; an older one is a writer that died, and yields.
const INTENT_PREFIX = 'intent-';
const INTENT_STALE_MS = 10000;
// The heartbeat's protocol: 2 is the build that yields while a snapshot write has failed.
const TAB_PROTOCOL = 2;
const TAB_HEARTBEAT = path.join('.agent', 'tabs', 'plugin.json');
const TAB_METRICS = path.join('.agent', 'tabs', 'plugin-metrics.jsonl');
const PAINT_EVERY_MS = 2000;
// `tab_plugin.HEARTBEAT_STALE_S` is 30 s: three missed heartbeats hand painting back to the drainer.
const HEARTBEAT_EVERY_MS = 10000;

function sameName(title, name) {
  return !!name && (title === name || title.endsWith(` ${name}`));
}

// The title an entry's tab should carry now: its label, or its stale label past valid_until.
function wantedTitle(entry, nowMs, staleReads) {
  const after = Date.parse(entry.show_after || '');
  const end = Date.parse(entry.show_until || '');
  const label = Number.isFinite(after) && nowMs < after && typeof entry.before_label === 'string'
    ? entry.before_label
    : Number.isFinite(end) && nowMs >= end && typeof entry.after_label === 'string'
      ? entry.after_label : entry.label;
  const until = Date.parse(entry.valid_until || '');
  const expired = !entry.owner_question && Number.isFinite(until) && nowMs > until && typeof entry.stale_label === 'string';
  const stale = staleReads ? staleReads.confirm(entry, expired, nowMs) : expired;
  // A clock-selected label keeps the same stale mark and name as the supplied label.
  const title = stale && label !== entry.label ? label.replace(' ', '? ') : stale ? entry.stale_label : label;
  return { title, stale, expired };
}

function expireProvisional(leaves, firstSeen, nowMs) {
  const titles = [CODEX_STARTING_TITLE, CLAUDE_STARTING_TITLE, '⏳ Codex manager', '⏳ Claude manager'];
  for (const leaf of leaves) {
    const view = leaf.view;
    if (!titles.includes(view?.state?.userTitle)) { firstSeen.delete(view); continue; }
    if (!firstSeen.has(view)) firstSeen.set(view, nowMs);
    if (nowMs - firstSeen.get(view) < PROVISIONAL_MS) continue;
    const next = structuredClone(view.state);
    next.userTitle = ''; // return to the client's title; keep the launch marker for a late claim
    view.state = next;
    firstSeen.delete(view);
  }
}

// Paint every entry's tab. Pure apart from the leaves' view state; returns the counts a metrics
// row carries and `shown`, the title each found tab carries afterwards, by key.
function paintTabs(leaves, entries, nowMs, staleReads) {
  const out = { attempted: 0, painted: 0, failed: 0, ambiguous: 0, stale: 0, errors: [], shown: {}, painted_labels: [] };
  for (const entry of Array.isArray(entries) ? entries : []) {
    if (!entry || typeof entry.key !== 'string' || typeof entry.label !== 'string') continue;
    let { title, stale, expired } = wantedTitle(entry, nowMs, staleReads);
    if (stale) out.stale += 1;
    const exact = [];
    const token = [];
    for (const leaf of leaves) {
      const view = leaf.view;
      const state = view && view.state;
      if (!state) continue;
      const shown = String((leaf.getDisplayText && leaf.getDisplayText()) || '');
      const user = String(state.userTitle || '');
      if (sameName(user, entry.match) || sameName(user, entry.generated_name)
        || (entry.generated && (sameName(shown, entry.match) || sameName(shown, entry.generated_name)))) {
        exact.push(view);
      } else if (entry.token && user.endsWith(entry.token)) {
        token.push(view);
      }
    }
    const matches = exact.length ? exact : token;
    if (matches.length !== 1) {
      if (matches.length > 1) out.ambiguous += 1;
      continue;
    }
    const view = matches[0];
    // A new painter keeps an already confirmed stale mark until a fresh read clears it.
    if (expired && !stale && view.state.userTitle === entry.stale_label) {
      title = entry.stale_label;
      stale = true;
      out.stale += 1;
    }
    if (String(view.state.userTitle || '') === title) {
      out.shown[entry.key] = title;
      continue;
    }
    out.attempted += 1;
    try {
      // the Terminal plugin's own rename path: a new view state with `userTitle` set
      const next = structuredClone(view.state);
      next.userTitle = title;
      view.state = next;
      if (String(view.state?.userTitle || '') !== title) throw new Error('the title did not take');
      out.painted += 1;
      out.painted_labels.push({ key: entry.key, label: title, provider: entry.provider || null });
      out.shown[entry.key] = title;
    } catch (error) {
      out.failed += 1;
      out.errors.push(`${entry.key}: ${error?.message || String(error)}`.slice(0, 300));
    }
  }
  return out;
}

class StaleReads {
  constructor() { this.entries = new Map(); }
  confirm(entry, stale, nowMs) {
    if (!stale) { this.entries.delete(entry.key); return false; }
    let prior = this.entries.get(entry.key);
    if (!prior || prior.label !== entry.label) prior = {label:entry.label, count:0, at:-Infinity};
    if (nowMs - prior.at >= PAINT_EVERY_MS) { prior.count++; prior.at = nowMs; }
    this.entries.set(entry.key, prior);
    return prior.count >= (entry.stale_reads || 1);
  }
}

// Polls live.json and paints. Writes `.agent/tabs/plugin.json` (the heartbeat that holds the
// Python eval push back, `tab_plugin.active`) at least every HEARTBEAT_EVERY_MS, and one
// `plugin-metrics.jsonl` row per poll that tried to change a tab or could not read the snapshot,
// for doctor's failure rate. `.agent/tabs-off`, the harness's kill switch, stops painting.
class TabPainter {
  constructor(workspace, vaultPath, version, fsImpl = require('fs')) {
    this.workspace = workspace;
    this.vaultPath = vaultPath;
    this.version = version;
    this.fs = fsImpl;
    this.beatAt = 0;
    this.lastShown = '';
    this.lastReadOk = null;
    this.lastYielded = null;
    this.lastOkAt = null;
    this.totals = { polls: 0, attempted: 0, failed: 0 };
    this.provisional = new WeakMap();
    this.staleReads = new StaleReads();
    this.snapshot = new JsonSnapshot(this.file(LIVE_JSON), fsImpl);
  }

  file(rel) {
    return path.join(this.vaultPath, rel);
  }

  // Whether any failed snapshot write stands: a file in `live-failed/`.
  // Returns { failed, error }. Only a missing folder means none stands; any other error reading
  // it (EACCES, EIO) counts as a failure standing, reported as a read error.
  // An intent younger than INTENT_STALE_MS: a write in flight, not a failure. Unreadable: stands.
  async intentInFlight(name, nowMs) {
    if (!name.startsWith(INTENT_PREFIX)) return false;
    try {
      const at = JSON.parse(await this.fs.promises.readFile(path.join(this.file(LIVE_FAILED), name), 'utf8')).at;
      return typeof at === 'number' && nowMs - at * 1000 <= INTENT_STALE_MS;
    } catch (error) {
      return error?.code === 'ENOENT'; // ended between the listing and this read
    }
  }

  async snapshotFailed(nowMs = Date.now()) {
    try {
      const names = (await this.fs.promises.readdir(this.file(LIVE_FAILED))).filter((name) => name.endsWith('.json'));
      const inFlight = await Promise.all(names.map((name) => this.intentInFlight(name, nowMs)));
      return { failed: inFlight.some((value) => !value), error: null };
    } catch (error) {
      if (error?.code === 'ENOENT') return { failed: false, error: null };
      return { failed: true, error: `live-failed: ${error?.message || String(error)}`.slice(0, 300) };
    }
  }

  poll(nowMs = Date.now()) {
    if (this.stopped) return Promise.resolve(null);
    if (this.pending) return this.pending;
    this.pending = this.pollOnce(nowMs).finally(() => { this.pending = null; });
    return this.pending;
  }

  stop() { this.stopped = true; }

  async pollOnce(nowMs) {
    const fs = this.fs;
    try { await fs.promises.stat(this.file('.agent')); }
    catch { return null; } // not a harness vault: write nothing
    this.totals.polls += 1;
    let result = { attempted: 0, painted: 0, failed: 0, ambiguous: 0, stale: 0, errors: [], shown: {} };
    let readError = null;
    // Health for `tab_plugin.active`: a poll that cannot read and apply
    // live.json says so, and the Python fallback paints instead of standing back for it.
    let readOk = false;
    let off = false;
    // A snapshot rewrite failed after live.json was derived (`live.WRITE_FAILED`):
    // its labels no longer hold the facts, so the plugin stops painting and says so; the Python
    // fallback, which knows the newer requests, paints until a good rewrite clears the record.
    // One painter: the fallback counts this plugin out only once its heartbeat says `yielded`.
    let yielded = false;
    let tabsOff = false;
    try { await fs.promises.stat(this.file(TABS_OFF)); tabsOff = true; }
    catch (error) { if (error?.code !== 'ENOENT') throw error; }
    const failure = tabsOff ? { failed: false, error: null } : await this.snapshotFailed(nowMs);
    if (tabsOff) {
      off = true;
    } else if (failure.failed) {
      // `painted` stays empty: the fallback's own record is then what the tabs show
      yielded = true;
      readError = failure.error;  // a record that could not be read: say so (read_ok stays false)
    } else {
      let snap = null;
      try {
        snap = await this.snapshot.read();
        if (!snap || !Array.isArray(snap.tabs)) readError = 'live.json: no tabs list';
      } catch (error) {
        if (error?.code !== 'ENOENT') readError = `live.json: ${error?.message || String(error)}`.slice(0, 300);
      }
      if (snap && Array.isArray(snap.tabs)) {
        if (this.stopped) return null;
        result = paintTabs(this.workspace.getLeavesOfType('terminal:terminal'), snap.tabs, nowMs, this.staleReads);
        readOk = true;
        this.lastOkAt = nowMs;
      }
    }
    if (this.stopped) return null;
    if (!off) expireProvisional(this.workspace.getLeavesOfType('terminal:terminal'), this.provisional, nowMs);
    this.totals.attempted += result.attempted;
    this.totals.failed += result.failed;
    try {
      // Publish without yielding so unload cannot interleave an older heartbeat.
      fs.mkdirSync(path.dirname(this.file(TAB_HEARTBEAT)), { recursive: true }); // .agent/tabs
      if (result.attempted || readError) {
        const row = {
          at: nowMs / 1000, attempted: result.attempted, painted: result.painted, failed: result.failed,
          ambiguous: result.ambiguous, stale: result.stale, read_error: readError, errors: result.errors,
          painted_labels: result.painted_labels || [],
        };
        fs.appendFileSync(this.file(TAB_METRICS), `${JSON.stringify(row)}\n`);
      }
      const shown = JSON.stringify(result.shown);
      const changed = shown !== this.lastShown || readOk !== this.lastReadOk || yielded !== this.lastYielded;
      if (nowMs - this.beatAt >= HEARTBEAT_EVERY_MS || changed) {
        const beat = {
          at: nowMs / 1000, at_iso: new Date(nowMs).toISOString(), version: this.version, pid: process.pid,
          protocol: TAB_PROTOCOL, off, yielded, read_ok: readOk, read_error: readError, last_ok_at: this.lastOkAt === null ? null : this.lastOkAt / 1000,
          painted: result.shown, ...this.totals,
        };
        const target = this.file(TAB_HEARTBEAT);
        // write, then rename, so the Python reader never sees half a heartbeat
        fs.writeFileSync(`${target}.tmp`, `${JSON.stringify(beat)}\n`);
        fs.renameSync(`${target}.tmp`, target);
        this.beatAt = nowMs;
        this.lastShown = shown;
        this.lastReadOk = readOk;
        this.lastYielded = yielded;
      }
    } catch (error) {
      console.warn('agent-terminal-focus: tab heartbeat failed', error);
    }
    return { ...result, read_error: readError, read_ok: readOk, off, yielded };
  }
}

function closeRestoredManagers(workspace) {
  const leaves = workspace.getLeavesOfType('terminal:terminal').filter(isManagerLeaf);
  for (const leaf of leaves) leaf.detach();
  return leaves.length;
}

class ConfirmClear extends Modal {
  constructor(app, data, onYes) {
    super(app);
    this.data = data;
    this.onYes = onYes;
  }

  onOpen() {
    const { contentEl } = this;
    contentEl.createEl('h3', { text: 'Clear leases?' });
    contentEl.createEl('p', {
      text: `This session touched a lease ${this.data.recent_minutes}m ago and may still be editing: ${this.data.areas.join(', ')}.`,
    });
    const row = contentEl.createDiv({ cls: 'modal-button-container' });
    row.createEl('button', { text: 'Clear anyway', cls: 'mod-warning' }).onclick = () => {
      this.close();
      this.onYes();
    };
    row.createEl('button', { text: 'Cancel' }).onclick = () => this.close();
  }

  onClose() {
    this.contentEl.empty();
  }
}

const LIVE_BLOCK = 'agent-live';

function liveAge(iso, nowMs) {
  const seconds = Math.max(0, Math.floor((nowMs - Date.parse(iso)) / 1000));
  if (!Number.isFinite(seconds)) return 'unknown age';
  return seconds < 60 ? `${seconds}s` : seconds < 3600 ? `${Math.floor(seconds / 60)}m` : `${Math.floor(seconds / 3600)}h`;
}

function livePast(iso, nowMs) { return !!iso && nowMs > Date.parse(iso); }
function liveDeferred(row, nowMs) { return !!row.show_after && nowMs < Date.parse(row.show_after); }
const textPart = (text) => ({ text: String(text ?? '—') });
const agePart = (at, prefix = '', suffix = '') => ({ at, prefix, suffix });
const linkPart = (text, href, internal = false) => ({ text, href, internal });
const mutedPart = (text) => ({ text: String(text), muted: true });
const leasePart = (row) => linkPart('Clear leases', `obsidian://agent-lease-clear?session=${encodeURIComponent(row.session_id)}`);

// Cells own their nodes. Structural changes patch only that cell; clock text has separate spans.
class LivePane {
  constructor(root, app) {
    this.app = app;
    this.root = root;
    this.ages = new Set();
    this.groups = new Map();
    this.warning = root.createEl('p');
    this.warning.style.minHeight = '1.5em';
    this.makeGroup('needs_you', 'Needs you', ['Action']);
    this.makeGroup('managers', 'Managers', ['Session', 'Doing', 'Since', 'Workers']);
    this.makeGroup('workers', 'Workers', ['Worker', 'State', 'Now', 'Goal']);
    this.makeGroup('features', 'Open features', ['Feature', 'Open tasks', 'Note']);
    this.makeGroup('tasks', 'Open tasks', ['Task']);
    // Rows that need action or are unknown stay open; healthy ones collapse into one line.
    this.healthHeading = root.createEl('h2', { text: 'Health' });
    this.makeGroup('health', '', ['Signal', 'Status']);
    this.healthDetails = root.createEl('details');
    this.healthSummary = this.healthDetails.createEl('summary');
    this.makeGroup('health_ok', '', ['Signal', 'Status'], this.healthDetails);
  }

  makeGroup(key, title, headers, parent = this.root) {
    const heading = title ? parent.createEl('h2', { text: title }) : null;
    const table = parent.createEl('table', { cls: 'agent-live-table' });
    table.style.width = '100%';
    table.style.tableLayout = 'fixed';
    const head = table.createEl('thead').createEl('tr');
    headers.forEach((text) => head.createEl('th', { text }));
    const body = table.createEl('tbody');
    const empty = parent.createEl('p');
    this.groups.set(key, { table, body, empty, heading, columns: headers.length, rows: new Map() });
  }

  patchCell(cell, parts) {
    const signature = JSON.stringify(parts);
    if (cell.signature === signature) return;
    for (const age of cell.ages || []) this.ages.delete(age);
    cell.ages = [];
    cell.el.replaceChildren();
    for (const part of parts) {
      const el = cell.el.createEl(part.href ? 'a' : part.bold ? 'strong' : 'span', { text: part.text || '', cls: part.internal ? 'internal-link' : part.href ? 'external-link' : '' });
      if (part.title) el.setAttr('title', part.title);
      if (part.muted) { el.style.color = 'var(--text-muted)'; el.style.display = 'block'; }
      if (part.href) {
        el.setAttr('href', part.href);
        if (part.internal) {
          el.setAttr('data-href', part.href);
          el.addEventListener('click', (event) => {
            event.preventDefault();
            this.app.workspace.openLinkText(part.href, '', event.metaKey || event.ctrlKey);
          });
        }
      }
      if ('at' in part) {
        const age = { el, ...part };
        this.ages.add(age);
        cell.ages.push(age);
      }
    }
    cell.signature = signature;
  }

  patchGroup(key, rows, emptyText) {
    const group = this.groups.get(key);
    const wanted = new Set(rows.map((r) => r.key));
    for (const [id, row] of group.rows) {
      if (wanted.has(id)) continue;
      for (const cell of row.cells) for (const age of cell.ages || []) this.ages.delete(age);
      row.el.remove();
      group.rows.delete(id);
    }
    const compare = (a, b) => String(a ?? '').localeCompare(String(b ?? ''), undefined, { numeric: true });
    rows.sort((a, b) => {
      for (let i = 0; i < Math.max(a.order?.length || 0, b.order?.length || 0); i++) {
        const order = compare(a.order?.[i], b.order?.[i]);
        if (order) return order;
      }
      return compare(a.feature, b.feature) || compare(a.key, b.key);
    });
    rows.forEach((spec, index) => {
      let row = group.rows.get(spec.key);
      if (!row) {
        const el = group.body.createEl('tr');
        el.setAttr('data-key', spec.key);
        row = { el, cells: spec.cells.map(() => ({ el: el.createEl('td') })) };
        row.cells.forEach((c) => { c.el.style.overflowWrap = 'anywhere'; c.el.style.verticalAlign = 'top'; });
        if (spec.span) {
          row.cells[0].el.colSpan = group.columns;
          row.cells[0].el.style.fontWeight = 'bold';
          row.cells[0].el.style.background = 'var(--background-secondary)';
        }
        group.rows.set(spec.key, row);
      }
      spec.cells.forEach((parts, i) => this.patchCell(row.cells[i], parts));
      if (group.body.children[index] !== row.el) group.body.insertBefore(row.el, group.body.children[index] || null);
    });
    const text = rows.length ? '' : emptyText;
    if (group.empty.textContent !== text) group.empty.setText(text);
  }

  update(snap, nowMs = Date.now(), error = '', status = this.statusSnapshot || {}, statusError = '') {
    this.snapshot = snap;
    this.error = error;
    this.statusSnapshot = status;
    this.statusError = statusError;
    const stale = (row) => !!error || livePast(snap.valid_until, nowMs) || livePast(row.valid_until, nowMs);
    const workers = snap.workers || [];
    const warning = error || statusError || (livePast(snap.valid_until, nowMs) ? '⚠ stale snapshot; states read unknown until rewritten' : workers.some((w) => ['queued', 'running'].includes(w.state)) && nowMs - Date.parse(snap.rendered_at) > 30000 ? '⚠ stale: no heartbeat from a running worker’s supervisor' : '');
    if (this.warning.textContent !== warning) this.warning.setText(warning);
    this.patchGroup('needs_you', (snap.needs_you || []).filter((r) => !liveDeferred(r, nowMs)).map((r) => {
      const parts = stale(r) ? [textPart('unknown (stale): ')] : [];
      if (r.kind === 'worker') {
        const w = workers.find((w) => w.id === r.id) || {};
        parts.push(linkPart(r.id, w.handoff, true), textPart(` ${w.repo || ''} ${WORKER_LIVE_ASK[r.state] || r.state} · `), linkPart('open', r.open));
      } else if (r.kind === 'orphaned') {
        parts.push(textPart(`feature ${String(r.feature).padStart(3, '0')} · ${r.count} result(s) with no live manager: ${(r.ids || []).join(', ')} · `), textPart(r.resume?.command ? `resume feature manager: ${r.resume.command}` : 'resume feature manager from the ribbon'));
      } else {
        parts.push(textPart(`${r.name} ${r.reason === 'question' ? 'has a question for you' : 'is waiting on you'} (`), agePart(r.since), textPart(')'));
        if (r.text) parts.push(mutedPart(r.text));
      }
      return { key: `${r.kind}:${r.id || r.session_id || r.feature}`, feature: r.feature, cells: [parts] };
    }), 'Nothing is waiting on you.');
    this.patchGroup('managers', (snap.managers || []).map((m) => {
      const session = [textPart(m.name)];
      if (m.background) session.push(mutedPart('Background session'));
      if (m.cost_usd) session.push(mutedPart(`$${Number(m.cost_usd).toFixed(2)}`));
      for (const p of [m, ...(m.parked || [])]) {
        if (p !== m) session.push(mutedPart(`parked in background: ${p.name} (${String(p.session_id).slice(0, 8)})${p.cost_usd ? ` · $${Number(p.cost_usd).toFixed(2)}` : ''}`));
        if (p.leases?.length) session.push(mutedPart(`holds ${p.leases.join(', ')}`));
        if (p.clear_leases) session.push(leasePart(p));
      }
      const state = stale(m) ? 'unknown' : liveDeferred(m, nowMs) ? m.before_state || 'working' : livePast(m.show_until, nowMs) ? m.after_state || 'idle' : m.state;
      const owner = !stale(m) && !liveDeferred(m, nowMs) && (m.attention ? ['question', 'approval'].includes(m.attention) : m.activity === 'waiting_on_you');
      const doing = [{ ...textPart(state), bold: owner }];
      if (stale(m)) doing.push(mutedPart(`stale; last seen ${m.state}`));
      const work = [textPart([...(m.running || []).map((id) => `⚙ ${id}`), ...(m.waiting || []).map((id) => `⏸ ${id}`)].join(' ') || '—')];
      if (m.manager_action) {
        work.push(mutedPart(`owes ${m.manager_action}`));
        const since = (m.manager_action_since || []).filter((s) => Number.isFinite(Date.parse(s))).sort()[0];
        if (since) work.push(agePart(since, ' · oldest '));
      }
      return { key: m.session_id, feature: m.feature, cells: [session, doing, [textPart(m.since ? new Date(m.since).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '—')], work] };
    }), 'No live manager sessions.');
    this.patchGroup('workers', workers.map((w) => {
      const running = ['queued', 'running'].includes(w.state);
      const state = [textPart(stale(w) ? 'unknown' : w.state)];
      if (stale(w)) state.push(mutedPart(`stale; last seen ${w.state}`));
      else state.push(agePart(running ? w.attempt_started || w.started : w.finished, ' ', running ? '' : ' ago'));
      if (running && w.supervisor_alive === false) state.push(mutedPart('supervisor gone'));
      else if (w.attempt > 1) state.push(mutedPart(`attempt ${w.attempt}`));
      const identity = [linkPart(w.id, w.handoff, true), mutedPart(w.repo || '')];
      if (w.cost_usd || w.tokens) identity.push(mutedPart(w.cost_usd ? `$${Number(w.cost_usd).toFixed(2)}` : `${w.tokens} tok`));
      return { key: w.id, feature: w.feature, cells: [identity, state, running ? [textPart(w.last_tool || '—'), agePart(w.last_activity, ' ', ' ago')] : [textPart('—')], [{text:String(w.goal || '').slice(0, 90),title:w.goal || ''}, textPart(' '), linkPart('open', w.open)]] };
    }), 'No live workers.');
    const features = (status.features || []).filter((f) => ['active', 'blocked', 'proposed'].includes(f.status));
    const statuses = ['active', 'blocked', 'proposed'];
    const ready = (f) => f.status === 'active' && f.open_tasks === 0 && !!f.done_tasks;
    const featureOrder = (f) => [statuses.indexOf(f.status), f.parked ? 2 : ready(f) ? 0 : 1, f.id];
    const featureLink = (f, suffix = '') => linkPart(`${String(f.id).padStart(3, '0')} ${f.title}${suffix}`, `${f.dir}/plan.md`, true);
    const featureRows = statuses.flatMap((status, rank) => {
      const members = features.filter((f) => f.status === status);
      return members.length ? [{ key: `sec:${status}`, span: true, order: [rank, -1], cells: [[textPart(`${status[0].toUpperCase() + status.slice(1)} (${members.length})`)]] }] : [];
    });
    featureRows.push(...features.map((f) => {
      const notes = [];
      if (f.parked) notes.push('parked');
      if (ready(f)) notes.push('ready to close');
      if (f.blocked_work?.length) notes.push(`active work: ${f.blocked_work.join('; ')}`);
      return { key: String(f.id), feature: f.id, order: featureOrder(f), cells: [[featureLink(f)], [textPart(f.open_tasks)], [textPart(notes.join(' · '))]] };
    }));
    this.patchGroup('features', featureRows, statusError || 'No open features.');
    const tasks = features.flatMap((f) => {
      const items = f.open_task_items || [];
      const order = featureOrder(f);
      return items.length ? [
        { key: `sec:${f.id}`, span: true, order: [...order, -1], cells: [[featureLink(f, ` (${items.length} open)`)]] },
        ...items.map((t) => ({ key: `${f.id}:${t.index}`, feature: f.id, order: [...order, t.index], cells: [[textPart(`#${t.index} ${t.text}`)]] })),
      ] : [];
    });
    this.patchGroup('tasks', tasks, statusError || 'No open tasks.');
    const taskCount = features.reduce((n, f) => n + (f.open_task_items || []).length, 0);
    for (const [key, title, count] of [['features', 'Open features', features.length], ['tasks', 'Open tasks', taskCount]]) {
      const heading = this.groups.get(key).heading;
      const text = `${title} (${count})`;
      if (heading.textContent !== text) heading.setText(text);
    }
    const health = liveHealthRows(snap.health, nowMs);
    const healthCells = (r) => {
      const status = [textPart(`${HEALTH_BADGE[r.state]} · ${r.headline || ''}`), ...(r.ages || []).map((a) => agePart(a.at, a.prefix, a.suffix))];
      if (r.note) status.push(textPart(` · ${r.note}`));
      if (r.meaning) status.push(mutedPart(r.meaning));
      if (r.action && r.state !== 'ok') status.push(mutedPart(`→ ${r.action}`));
      return { key: r.key, order: r.order, cells: [[{ ...textPart(r.title), bold: r.state === 'action' }], status] };
    };
    const open = health.filter((r) => r.state !== 'ok');
    const ok = health.filter((r) => r.state === 'ok');
    this.patchGroup('health', open.map(healthCells), '');
    this.patchGroup('health_ok', ok.map(healthCells), '');
    this.groups.get('health').table.style.display = open.length ? '' : 'none';
    this.healthDetails.style.display = ok.length ? '' : 'none';
    const need = open.filter((r) => r.state === 'action').length;
    const unknown = open.length - need;
    const heading = open.length
      ? `Health: ${[need && `${need} need${need === 1 ? 's' : ''} action`, unknown && `${unknown} unknown`].filter(Boolean).join(', ')}`
      : 'Health: all clear';
    if (this.healthHeading.textContent !== heading) this.healthHeading.setText(heading);
    const summary = `${ok.length} ok: ${ok.map((r) => r.brief || r.title).join(', ')}`;
    if (this.healthSummary.textContent !== summary) this.healthSummary.setText(summary);
    for (const age of this.ages) {
      const text = `${age.prefix}${liveAge(age.at, nowMs)}${age.suffix}`;
      if (age.el.textContent !== text) age.el.setText(text);
    }
  }

  tick(nowMs = Date.now()) { if (this.snapshot) this.update(this.snapshot, nowMs, this.error, this.statusSnapshot, this.statusError); }
}
const WORKER_LIVE_ASK = { done: 'finished — review, then push or ack', question: 'has a question', blocked: 'is blocked', failed: 'failed' };
const HEALTH_BADGE = { ok: 'ok', action: 'needs action', unknown: 'unknown' };

// Python decides each row and its expired form; this applies only the clock. A row past
// valid_until takes its expired form or, with none, folds into its machine's "no report" row,
// which waits for show_after. A missing health block is unknown, never healthy.
function liveHealthRows(health, nowMs) {
  if (!health || !Array.isArray(health.rows)) {
    return [{ key: 'health:', title: 'Health', state: 'unknown', headline: 'not reported in this snapshot', order: [1] }];
  }
  return health.rows.filter((r) => !liveDeferred(r, nowMs)).flatMap((r) => {
    const row = livePast(r.valid_until, nowMs) ? r.expired && { ...r, ...r.expired } : r;
    return row ? [{ ...row, state: row.state in HEALTH_BADGE ? row.state : 'unknown' }] : [];
  });
}

class AgentLiveBlock extends (MarkdownRenderChild || class {}) {
  constructor(el, plugin) { super(el); this.el = el; this.plugin = plugin; }
  onload() { this.disposePane = this.plugin.mountLive(this.el); }
  onunload() { this.disposePane?.(); }
}

module.exports = class AgentTerminalFocus extends Plugin {
  mountLive(el) {
    const pane = new LivePane(el, this.app);
    this.livePanes.add(pane);
    this.refreshLive();
    return () => this.livePanes.delete(pane);
  }

  refreshLive() {
    if (this.liveRefresh) { this.liveRefreshAgain = true; return this.liveRefresh; }
    this.liveRefresh = (async () => {
      do {
        this.liveRefreshAgain = false;
        await this.refreshLiveOnce();
      } while (this.liveRefreshAgain);
    })().finally(() => { this.liveRefresh = null; });
    return this.liveRefresh;
  }

  async refreshLiveOnce() {
    if (!this.livePanes.size) return;
    let snap = this.liveSnapshot || {};
    let error = '';
    try {
      snap = await this.liveReader.read();
      this.liveSnapshot = snap;
      if ((await this.tabPainter?.snapshotFailed())?.failed) error = '⚠ live snapshot write failed; states read unknown';
    } catch { error = '⚠ live snapshot unreadable; states read unknown'; }
    let status = this.statusSnapshot || {};
    let statusError = '';
    try {
      const nextStatus = await this.statusReader.read();
      if (!Array.isArray(nextStatus?.features)) throw new Error('no features');
      status = this.statusSnapshot = nextStatus;
    } catch { statusError = '⚠ feature/task snapshot unreadable; showing last recorded counts'; }
    let renderFailed = false;
    for (const pane of this.livePanes) {
      try { pane.update(snap, Date.now(), error, status, statusError); }
      catch {
        renderFailed = true;
        pane.warning.setText('⚠ live pane render failed; reload the plugin');
        if (!this.liveRenderFailed) {
          try {
            const fs = require('fs');
            const file = path.join(this.app.vault.adapter.getBasePath(), '.agent', 'tabs', 'pane-metrics.jsonl');
            fs.mkdirSync(path.dirname(file), {recursive:true});
            fs.appendFileSync(file, `${JSON.stringify({at:Date.now()/1000,failed:1})}\n`);
          } catch { /* An unwritable metrics file cannot repair the view. */ }
        }
        this.liveRenderFailed = true;
      }
    }
    this.liveRenderFailed = renderFailed;
  }
  async clearLeases(session) {
    if (!/^[A-Za-z0-9._-]{1,128}$/.test(session || '')) {
      new Notice('Clear leases: not a session id');
      return;
    }
    const vaultPath = this.app.vault.adapter.getBasePath();
    const report = (result) => {
      if (result.error) new Notice(`Clear leases failed: ${result.error}`);
      else if (result.data.cleared.length) new Notice(`Cleared ${result.data.cleared.join(', ')}`);
      else new Notice(`Nothing cleared for ${session}`);
    };
    const first = await runLeaseClear(vaultPath, session, false);
    if (first.data?.needs_confirm) {
      new ConfirmClear(this.app, first.data, async () => report(await runLeaseClear(vaultPath, session, true))).open();
      return;
    }
    report(first);
  }

  onload() {
    this.livePanes = new Set();
    this.liveReader = new JsonSnapshot(path.join(this.app.vault.adapter.getBasePath(), LIVE_JSON));
    this.statusReader = new JsonSnapshot(path.join(this.app.vault.adapter.getBasePath(), '.agent/status.json'));
    this.registerMarkdownCodeBlockProcessor?.(LIVE_BLOCK, (_source, el, ctx) => ctx.addChild(new AgentLiveBlock(el, this)));
    this.registerDomEvent(window, 'keydown', (event) => editorFind(this.app.workspace, event), { capture: true });
    this.registerEvent(this.app.vault.on?.('modify', (file) => { if (['.agent/live.json', '.agent/status.json'].includes(file.path)) this.refreshLive(); }));
    // Hidden .agent files may not enter the vault index. Watch their directory for atomic renames.
    try {
      const watcher = require('fs').watch(path.join(this.app.vault.adapter.getBasePath(), '.agent'), (_event, file) => {
        if (['live.json', 'status.json'].includes(String(file))) this.refreshLive();
      });
      watcher.on('error', () => this.refreshLive());
      this.register(() => watcher.close());
    } catch { /* The 2 s poll also handles a directory created after plugin load. */ }
    this.registerInterval(window.setInterval(() => this.refreshLive(), PAINT_EVERY_MS));
    this.register(() => this.livePanes.clear());
    this.registerObsidianProtocolHandler('agent-lease-clear', (params) => this.clearLeases(params.session));
    if (['darwin', 'linux', 'win32'].includes(process.platform)) {
      addIcon('agent-openai', OPENAI_ICON);
      addIcon('agent-claude', CLAUDE_ICON);
      this.addRibbonIcon('agent-openai', 'New Codex manager', () => this.openManager('codex'));
      this.addRibbonIcon('agent-claude', 'New Claude manager', () => this.openManager('claude'));
    }

    // An Obsidian restart brings back every manager tab from the saved workspace and starts a
    // fresh agent in each. The owner wants a fresh start, with no session resumed, so the
    // ribbon's manager tabs are closed once the restored layout is ready. Only at app
    // start: a plugin enabled or reloaded mid-session finds the layout already ready and leaves
    // the live managers alone. Any process chain a close leaves on Windows is ended by the sweep.
    if (!this.app.workspace.layoutReady) {
      this.app.workspace.onLayoutReady(() => closeRestoredManagers(this.app.workspace));
    }

    // Every terminal, new or restored after a restart, gets the hyperlink handler. The
    // emulator appears asynchronously, so layout changes and a slow sweep both apply it.
    const guarded = new Set();
    const handler = linkHandler(this.app.vault.getName?.() || path.basename(this.app.vault.adapter.getBasePath()), this.app);
    const patchAll = () => this.app.workspace.getLeavesOfType('terminal:terminal').forEach((leaf) => {
      patchLinkHandler(leaf, handler);
      guardCodexRightClick(leaf, guarded, (dispose) => this.register(dispose));
    });
    this.app.workspace.onLayoutReady(patchAll);
    let patchTimer = null;
    this.registerEvent(this.app.workspace.on('layout-change', () => {
      if (patchTimer !== null) clearTimeout(patchTimer);
      patchTimer = setTimeout(() => { patchTimer = null; patchAll(); }, 250);
    }));
    this.register(() => { if (patchTimer !== null) clearTimeout(patchTimer); });
    this.registerInterval(window.setInterval(patchAll, 2000));

    // Tab labels from live.json. A paint error is counted, never thrown.
    const painter = new TabPainter(this.app.workspace, this.app.vault.adapter.getBasePath(), this.manifest?.version);
    this.tabPainter = painter;
    this.register(() => painter.stop());
    const paint = async () => {
      try {
        await painter.poll();
      } catch (error) {
        console.warn('agent-terminal-focus: tab paint failed', error);
      }
    };
    this.app.workspace.onLayoutReady(paint);
    this.registerInterval(window.setInterval(paint, PAINT_EVERY_MS));

    if (process.platform === 'win32') this.startOrphanSweep();

    let pending = null;
    let lastHeader = null;
    let tabPointerAt = 0;
    const onPointerDown = (event) => {
      const target = event.target;
      lastHeader = target instanceof Element ? target.closest('.workspace-tab-header') : null;
      tabPointerAt = lastHeader ? performance.now() : 0;
    };
    this.registerDomEvent(document, 'pointerdown', onPointerDown, { capture: true });
    this.registerEvent(this.app.workspace.on('active-leaf-change', (leaf) => {
      if (pending !== null) {
        clearTimeout(pending);
        pending = null;
      }
      const clickedTab = lastHeader !== null
        && performance.now() - tabPointerAt < 1000
        && (leaf?.tabHeaderEl
          ? lastHeader === leaf.tabHeaderEl
          : lastHeader.getAttribute('data-type') === 'terminal:terminal');
      if (!clickedTab) return;
      lastHeader = null;
      tabPointerAt = 0;
      if (!leaf || leaf.view?.getViewType() !== 'terminal:terminal') return;

      pending = setTimeout(() => {
        pending = null;
        if (this.app.workspace.activeLeaf !== leaf) return;
        const view = leaf.view;
        if (view?.getViewType() !== 'terminal:terminal') return;
        if (document.querySelector('.modal-container')) return;
        if (view.containerEl?.contains(document.activeElement)) return;
        view.focus();
      }, 0);
    }));
    this.register(() => {
      if (pending !== null) clearTimeout(pending);
    });
  }

  // At load (leftovers from the last quit), 2 s after a layout change (a closed tab), and each
  // minute. One sweep at a time; a failure is logged, and shown once per Obsidian session.
  startOrphanSweep() {
    let running = false;
    let warned = false;
    let debounce = null;
    const sweep = async () => {
      if (running) return;
      running = true;
      try {
        const reaped = await reapOrphans();
        if (reaped.length) console.info(`agent-terminal-focus: ended closed-tab terminal chains ${reaped.join(', ')}`);
      } catch (error) {
        console.warn('agent-terminal-focus: orphan sweep failed', error);
        if (!warned) new Notice(`Manager cleanup failed: ${error?.message || String(error)}`);
        warned = true;
      } finally {
        running = false;
      }
    };
    this.app.workspace.onLayoutReady(sweep);
    this.registerEvent(this.app.workspace.on('layout-change', () => {
      if (debounce !== null) clearTimeout(debounce);
      debounce = setTimeout(() => {
        debounce = null;
        sweep();
      }, 2000);
    }));
    this.register(() => {
      if (debounce !== null) clearTimeout(debounce);
    });
    this.registerInterval(window.setInterval(sweep, 60000));
  }

  async openManager(client) {
    const title = client === 'codex' ? CODEX_STARTING_TITLE : CLAUDE_STARTING_TITLE;
    const launchMarker = `${client}:${randomUUID()}`;
    const leaf = this.app.workspace.getLeaf('tab');
    try {
      // Terminal 3.27.2's integrated view-state shape; pinned in PLUGIN_SOURCES.
      await leaf.setViewState({
        type: 'terminal:terminal',
        active: true,
        state: {
          'terminal:terminal': {
            cwd: this.app.vault.adapter.getBasePath(),
            focus: true,
            profile: managerProfile(client, launchMarker, this.app.vault.adapter.getBasePath()),
            profileSourceId: null,
            serial: null,
            userTitle: title,
          },
        },
      });
      if (leaf.view?.getViewType() !== 'terminal:terminal' || leaf.view.state?.userTitle !== title) {
        throw new Error('Terminal did not accept the manager profile and title');
      }
      // A manager tab is long-lived: pinned, a click on a note elsewhere cannot replace it.
      // Only after the terminal accepted the profile, so a failed
      // open never leaves a pinned empty tab.
      leaf.setPinned(true);
    } catch (error) {
      leaf.detach();
      new Notice(`Could not open ${client} manager terminal: ${error?.message || String(error)}`);
    }
  }
};

// For tests: the pure selection and the query text.
module.exports.orphanChains = orphanChains;
module.exports.PROCESS_QUERY = PROCESS_QUERY;
module.exports.isManagerLeaf = isManagerLeaf;
module.exports.managerProfile = managerProfile;
module.exports.guardCodexRightClick = guardCodexRightClick;
module.exports.paintTabs = paintTabs;
module.exports.TabPainter = TabPainter;
module.exports.expireProvisional = expireProvisional;
module.exports.LivePane = LivePane;
module.exports.linkHandler = linkHandler;
module.exports.StaleReads = StaleReads;
