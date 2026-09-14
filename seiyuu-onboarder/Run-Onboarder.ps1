# Run-Onboarder.ps1
# Launches the Seiyuu Onboarding GUI.
#
# Usage (from PowerShell, any working directory):
#   & "C:\JP-Audiobook-Generator\seiyuu-onboarder\Run-Onboarder.ps1"
#
# Or, from inside this folder:
#   .\Run-Onboarder.ps1
#
# NOTE: this tool has its OWN uv project (pyproject.toml / uv.lock / .venv)
# right here, separate from the audiobook generator's venv in the folder
# above and from the heavy Irodori-TTS venv. It needs only customtkinter and
# pillow - Whisper is NOT installed here, because the tool shells out to the
# existing transcription venv in C:\Transcribe instead of duplicating it.
#
# The "irodori_root", "whisper_exe" and "transcript_root" values in
# settings.json are what point this tool at those other installs.

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppScript = Join-Path $ScriptDir "app.py"
$PyprojectPath = Join-Path $ScriptDir "pyproject.toml"

if (-not (Test-Path $AppScript)) {
    Write-Error "Could not find app.py at: $AppScript"
    exit 1
}

$uvCmd = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uvCmd) {
    Write-Error "'uv' was not found on PATH. Please install uv or add it to PATH."
    exit 1
}

if (-not (Test-Path $PyprojectPath)) {
    Write-Error "No pyproject.toml found in: $ScriptDir"
    exit 1
}

Push-Location $ScriptDir
try {
    # No --no-sync: this is a small, fast-syncing local venv, so let uv
    # reconcile the lock and the venv if they drift.
    & uv run python $AppScript
}
finally {
    Pop-Location
}
