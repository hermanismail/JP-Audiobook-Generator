# Run-Repair.ps1
# Launches the Chapter Repair GUI.
#
# Usage (from PowerShell, any working directory):
#   & "C:\JP-Audiobook-Generator\chapter-repair\Run-Repair.ps1"
#
# NOTE: this tool has its OWN uv project (pyproject.toml / uv.lock / .venv)
# right here. It needs customtkinter, pillow and mutagen - no torch and no
# whisper, because the TTS runs in the Irodori-TTS venv (through the
# audition tool's worker) and transcription shells out to C:\Transcribe.
#
# It REWRITES PUBLISHED FILES. Every repair backs up the .m4a, .sync.json,
# .srt and the FLAC master first - see "backup_root" in settings.json.

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppScript = Join-Path $ScriptDir "app.py"

if (-not (Test-Path $AppScript)) {
    Write-Error "Could not find app.py at: $AppScript"
    exit 1
}

$uvCmd = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uvCmd) {
    Write-Error "'uv' was not found on PATH. Please install uv or add it to PATH."
    exit 1
}

Push-Location $ScriptDir
try {
    & uv run python $AppScript
}
finally {
    Pop-Location
}
