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
# inside the venv managed by 'uv' in the uv project folder (see UvProjectDir
# below / "uv_project_dir" in settings.json). So this script runs the GUI via
# `uv run --no-sync python <gui path>` from that folder, the same way
# run_audiobook.py itself is normally launched.

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$GuiScript = Join-Path $ScriptDir "gui_settings.py"
$SettingsPath = Join-Path $ScriptDir "settings.json"

if (-not (Test-Path $GuiScript)) {
    Write-Error "Could not find gui_settings.py at: $GuiScript"
    exit 1
}

# Default uv project folder (fallback if settings.json is missing/unreadable)
$UvProjectDir = "C:\Irodori-TTS"

if (Test-Path $SettingsPath) {
    try {
        $settingsJson = Get-Content -Raw -Path $SettingsPath | ConvertFrom-Json
        if ($settingsJson.uv_project_dir) {
            $UvProjectDir = $settingsJson.uv_project_dir
        }
    }
    catch {
        Write-Warning "Could not parse settings.json, using default uv project folder: $UvProjectDir"
    }
}

if (-not (Test-Path $UvProjectDir)) {
    Write-Error "uv project folder not found: $UvProjectDir`nUpdate 'uv_project_dir' in settings.json, or edit `$UvProjectDir at the top of this script."
    exit 1
}

$uvCmd = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uvCmd) {
    Write-Error "'uv' was not found on PATH. Please install uv or add it to PATH."
    exit 1
}

Push-Location $UvProjectDir
try {
    & uv run --no-sync python $GuiScript
}
finally {
    Pop-Location
}
