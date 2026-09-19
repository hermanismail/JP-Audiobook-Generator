# Create-Shortcut.ps1
# One-time helper: creates a "Scene Illustrator.lnk" shortcut on your Desktop,
# pointing to the silent launcher (Launch-SceneIllustrator-Silent.vbs).
#
# Usage: run this ONCE, either by:
#   - Right-clicking this file -> "Run with PowerShell", or
#   - From a PowerShell prompt:
#       powershell -ExecutionPolicy Bypass -File "C:\JP-Audiobook-Generator\scene-illustrator\Create-Shortcut.ps1"
#
# After it runs, a shortcut appears on your Desktop. Pin THAT shortcut to the
# taskbar (right-click it -> Pin to taskbar, or drag it onto the taskbar).
#
# The icon is teal, unlike every other tool in this repo.

$Root = "C:\JP-Audiobook-Generator\scene-illustrator"
$TargetVbs = Join-Path $Root "Launch-SceneIllustrator-Silent.vbs"
$IconPath = Join-Path $Root "illustrator_icon.ico"
$DesktopPath = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $DesktopPath "Scene Illustrator.lnk"

if (-not (Test-Path $TargetVbs)) {
    Write-Error "Could not find: $TargetVbs"
    exit 1
}

$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = "$env:WINDIR\System32\wscript.exe"
$Shortcut.Arguments = "`"$TargetVbs`""
$Shortcut.WorkingDirectory = $Root
$Shortcut.Description = "Scene Illustrator - zen-mode images per chapter"
if (Test-Path $IconPath) {
    $Shortcut.IconLocation = $IconPath
}
$Shortcut.Save()

Write-Host "Shortcut created at: $ShortcutPath"
Write-Host "Now right-click it and choose 'Pin to taskbar' (or drag it onto the taskbar)."
