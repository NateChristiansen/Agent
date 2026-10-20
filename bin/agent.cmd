@echo off
rem bin\agent.cmd: the Windows counterpart of bin/agent. The caller's cwd is preserved and
rem AGENT_ROOT tells the CLI where the harness lives. Prefers the synced venv (fast start);
rem falls back to `uv run`, which syncs the environment on first use. Once a venv exists the
rem fallback is `uv run --no-sync`: a sync there fails with os error 32 while a live process holds
rem a venv file. Setup syncs.
setlocal
for %%I in ("%~dp0..") do set "AGENT_ROOT=%%~fI"
if exist "%AGENT_ROOT%\.venv\Scripts\python.exe" (
  "%AGENT_ROOT%\.venv\Scripts\python.exe" -I -m agent_cli %*
) else if exist "%AGENT_ROOT%\.venv\" (
  uv run --no-sync --quiet --project "%AGENT_ROOT%" agent %*
) else (
  uv run --quiet --project "%AGENT_ROOT%" agent %*
)
exit /b %ERRORLEVEL%
