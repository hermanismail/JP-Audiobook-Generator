# Run-Settings.ps1
# Launches the JP Audiobook Generator settings GUI.
#
# Usage (from PowerShell, any working directory):
#   & "C:\JP-Audiobook-Generator\Run-Settings.ps1"
#
# Or, from inside the working folder:
#   .\Run-Settings.ps1
#
# NOTE: This environment has no standalone system Python - Python only exists
# inside uv-managed venvs. The GUI now has its OWN uv project (pyproject.toml
# / uv.lock / .venv) living right here in C:\JP-Audiobook-Generator, separate
# from the Irodori-TTS venv used to actually run the audiobook pipeline.
#
# This keeps GUI-only dependencies (customtkinter, pillow, etc.) isolated from
# the heavier ML/TTS environment in C:\Irodori-TTS. The "uv_project_dir"
# setting in settings.json is unrelated to this script - it's only used by
# gui_settings.py's "Save & Run" button to launch run_audiobook.py in the
# Irodori-TTS venv.

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$GuiScript = Join-Path $ScriptDir "gui_settings.py"
$PyprojectPath = Join-Path $ScriptDir "pyproject.toml"

if (-not (Test-Path $GuiScript)) {
    Write-Error "Could not find gui_settings.py at: $GuiScript"
    exit 1
}

$uvCmd = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uvCmd) {
    Write-Error "'uv' was not found on PATH. Please install uv or add it to PATH."
    exit 1
}

if (-not (Test-Path $PyprojectPath)) {
    Write-Error "No pyproject.toml found in: $ScriptDir`nRun 'uv init --no-workspace' and 'uv add customtkinter pillow' in this folder first (see setup notes)."
    exit 1
}

Push-Location $ScriptDir
try {
    # No --no-sync here (unlike the Irodori-TTS launch) - this is a small,
    # fast-syncing local venv, so let uv auto-sync if the lock/venv drift.
    & uv run python $GuiScript
}
finally {
    Pop-Location
}
