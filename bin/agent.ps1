# bin/agent.ps1: the PowerShell counterpart of bin/agent. The caller's cwd is preserved and
# AGENT_ROOT tells the CLI where the harness lives. Prefers the synced venv (fast start);
# falls back to `uv run`, which syncs the environment on first use. The venv's python runs the
# CLI as a module, not agent.exe, which `uv sync` cannot replace while any process runs it.
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$exe = Join-Path $root '.venv\Scripts\python.exe'
$before = $env:AGENT_ROOT
$env:AGENT_ROOT = $root
try {
    if (Test-Path $exe) {
        & $exe -I -m agent_cli @args
    } else {
        uv run --quiet --project $root agent @args
    }
    $code = $LASTEXITCODE
} finally {
    $env:AGENT_ROOT = $before
}
exit $code
