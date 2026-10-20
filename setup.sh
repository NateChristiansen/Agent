#!/usr/bin/env bash
# setup.sh: one command to get this harness running on macOS or Linux.
# Makes sure uv is there, syncs the project, then hands over to `agent setup`, which checks
# every other requirement, installs what it can, prints an exact guide for the rest, and
# configures the vault. Re-runnable.
#
# One yes covers everything: when uv is missing, the question asked here also
# answers `agent setup`'s install question, by passing --yes on. --yes skips the question;
# --check installs nothing.
#
# Windows: use setup.cmd. From Git Bash, this script hands over to setup.ps1, so the Windows
# paths (junctions, the Windows plugin config) are the ones that run.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

case "$(uname -s)" in
  MINGW* | MSYS* | CYGWIN*)
    echo "Windows shell detected; running setup.ps1 through PowerShell..."
    exec powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$ROOT/setup.ps1" "$@"
    ;;
esac

# Expand as ${args[@]+"${args[@]}"}: macOS ships bash 3.2, where "${args[@]}" of an empty array
# is an unbound variable under `set -u`, and a plain `./setup.sh` died on it (2026-09-29).
args=("$@")
check_only=0
assume_yes=0
for a in "$@"; do
  case "$a" in
    --check) check_only=1 ;;
    --yes) assume_yes=1 ;;
  esac
done

if ! command -v uv >/dev/null 2>&1; then
  if [ "$check_only" = 1 ]; then
    echo "uv is missing. Install it with: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
  fi
  if [ "$assume_yes" = 0 ]; then
    read -r -p "uv is missing. Install uv and every other missing requirement? [Y/n] " answer
    case "$answer" in
      "" | [Yy]*) args+=("--yes") ;;
      *)
        echo "Nothing installed. Install uv with: curl -LsSf https://astral.sh/uv/install.sh | sh"
        exit 1
        ;;
    esac
  fi
  echo "installing uv (https://docs.astral.sh/uv/)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # the installer drops uv here and prints a line about restarting the shell
  export PATH="$HOME/.local/bin:$PATH"
fi

if [ "$check_only" = 1 ]; then
  # --check writes nothing, so no `uv sync`, and `--no-sync` stops `uv run` syncing implicitly.
  if [ ! -d "$ROOT/.venv" ]; then
    echo "uv is present, but the project is not synced yet. Run ./setup.sh (without --check) once."
    exit 1
  fi
  exec uv run --no-sync agent --root "$ROOT" setup ${args[@]+"${args[@]}"}
fi

uv sync
exec uv run agent --root "$ROOT" setup ${args[@]+"${args[@]}"}
