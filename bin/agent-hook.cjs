// Shared command-hook launcher. The inline loader in both client configs resolves this file
// from CLAUDE_PROJECT_DIR or the git root, including when a client starts in a subdirectory.
// Node is a setup requirement. No shell, execution-policy change, or dependency sync is needed.
const { existsSync } = require('fs');
const { join, resolve } = require('path');
const { spawnSync } = require('child_process');

function main(args = process.argv.slice(require.main === module ? 2 : 1)) {
  const root = resolve(__dirname, '..');
  const probe = args.includes('--probe');
  const hook = args[0] === 'agent' && args[1] === 'hook';
  let fallback = false;
  let executable, forwarded;
  if (args[0] === 'agent') {
    // Windows runs the venv's python, not agent.exe, which `uv sync` cannot replace while any
    // hook or server runs it.
    const windows = process.platform === 'win32';
    const cli = windows
      ? join(root, '.venv', 'Scripts', 'python.exe')
      : join(root, '.venv', 'bin', 'agent');
    executable = existsSync(cli) ? cli : 'uv';
    fallback = !existsSync(cli);
    forwarded = existsSync(cli)
      ? [...(windows ? ['-I', '-m', 'agent_cli'] : []), ...args.slice(1)]
      : ['run', '--no-sync', '--project', root, 'agent', ...args.slice(1)];
  } else {
    throw new Error('Expected agent');
  }
  const result = spawnSync(executable, forwarded, {
    cwd: root,
    env: { ...process.env, AGENT_ROOT: root },
    stdio: fallback ? ['inherit', 'pipe', 'pipe'] : 'inherit',
    windowsHide: true,
    maxBuffer: 16 * 1024 * 1024,
  });
  // An unsynced clone must not flood every prompt with uv errors. All ordinary handlers,
  // including guards, fail open when unavailable, as Python's normal hook dispatcher does.
  // Doctor's strict probes instead fail visibly; they cannot be mistaken for working guards.
  if (result.error || (fallback && result.status !== 0)) {
    if (hook && !probe) return 0;
    if (result.stderr) process.stderr.write(result.stderr);
    throw result.error || new Error(`CLI unavailable (exit ${result.status})`);
  }
  if (fallback) {
    if (result.stdout) process.stdout.write(result.stdout);
    if (result.stderr) process.stderr.write(result.stderr);
  }
  return result.status ?? 1;
}

try {
  process.exitCode = main();
} catch (error) {
  process.stderr.write(`agent hook launcher: ${error.message}\n`);
  process.exitCode = 1;
}
