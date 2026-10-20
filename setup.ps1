# setup.ps1: one command to get this harness running on Windows (PowerShell 5.1+).
# Run it through setup.cmd (or `powershell -ExecutionPolicy Bypass -File .\setup.ps1`), which
# leaves the machine's execution policy alone.
#
# Makes sure uv is there, syncs the project, then hands over to `agent setup`, which checks
# every other requirement, installs what it can, prints an exact guide for the rest, and
# configures the vault. Re-runnable.
#
# One yes covers everything: when uv is missing, the question asked here also
# answers `agent setup`'s install question, by passing --yes on. --yes skips the question;
# --check installs nothing. On Windows the CLI is bin\agent.cmd (or bin\agent.ps1).
$ErrorActionPreference = 'Stop'

Set-Location -Path $PSScriptRoot

$passArgs = @($args)
$checkOnly = $passArgs -contains '--check'
$assumeYes = $passArgs -contains '--yes'

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    if ($checkOnly) {
        Write-Host 'uv is missing. Install it with: winget install --id astral-sh.uv -e'
        exit 1
    }
    if (-not $assumeYes) {
        $answer = Read-Host 'uv is missing. Install uv and every other missing requirement? [Y/n]'
        if ($answer -and $answer -notmatch '^[Yy]') {
            Write-Host 'Nothing installed. Install uv with: winget install --id astral-sh.uv -e'
            exit 1
        }
        $passArgs += '--yes'
    }
    Write-Host 'installing uv (https://docs.astral.sh/uv/)...'
    powershell -NoProfile -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Host 'uv did not install. Try: winget install --id astral-sh.uv -e, then run setup.cmd again.'
        exit 1
    }
}

if ($checkOnly) {
    # --check writes nothing, so no `uv sync`, and `--no-sync` stops `uv run` syncing implicitly.
    if (-not (Test-Path (Join-Path $PSScriptRoot '.venv'))) {
        Write-Host 'uv is present, but the project is not synced yet. Run setup.cmd (without --check) once.'
        exit 1
    }
    uv run --no-sync agent --root $PSScriptRoot setup @passArgs
    exit $LASTEXITCODE
}

uv sync
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
uv run agent --root $PSScriptRoot setup @passArgs
exit $LASTEXITCODE
