# Run-Profiler.ps1
# Launches the Book Profiler GUI.
#
# Usage (from PowerShell, any working directory):
#   & "C:\JP-Audiobook-Generator\book-profiler\Run-Profiler.ps1"
#
# Or, from inside this folder:
#   .\Run-Profiler.ps1
#
# NOTE: this tool has its OWN uv project (pyproject.toml / uv.lock / .venv)
# right here. It needs only customtkinter and pillow - no torch and no
# irodori_tts, because the TTS itself runs in the heavy Irodori-TTS venv,
# reached by running the generator's irodori_batch.py through uv, and
# Whisper runs from the C:\Transcribe venv.

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
