# Run-SceneIllustrator.ps1
# Launches the Scene Illustrator GUI.
#
# Usage (from PowerShell, any working directory):
#   & "C:\JP-Audiobook-Generator\scene-illustrator\Run-SceneIllustrator.ps1"
#
# NOTE: this tool has its OWN uv project (pyproject.toml / uv.lock / .venv)
# right here. It needs only customtkinter and pillow - the LLM runs in
# llama.cpp's llama-server and the images in F:\ComfyUI, both over HTTP.

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
    & uv run python $AppScript
}
finally {
    Pop-Location
}
