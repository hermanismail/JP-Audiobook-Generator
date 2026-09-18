# Create-Shortcut.ps1
# One-time helper: creates a "Dynamic Repair.lnk" shortcut on your Desktop,
# pointing to the silent launcher (Launch-DynamicRepair-Silent.vbs).
#
# Usage: run this ONCE, either by:
#   - Right-clicking this file -> "Run with PowerShell", or
#   - From a PowerShell prompt:
#       powershell -ExecutionPolicy Bypass -File "C:\JP-Audiobook-Generator\dynamic-repair\Create-Shortcut.ps1"
#
# After it runs, a shortcut appears on your Desktop. Pin THAT shortcut to the
# taskbar (right-click it -> Pin to taskbar, or drag it onto the taskbar).
#
# The icon is coral, unlike every other tool in this repo, so they all tell
# apart on the taskbar.

$Root = "C:\JP-Audiobook-Generator\dynamic-repair"
$TargetVbs = Join-Path $Root "Launch-DynamicRepair-Silent.vbs"
$IconPath = Join-Path $Root "dynrepair_icon.ico"
$DesktopPath = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $DesktopPath "Dynamic Repair.lnk"

if (-not (Test-Path $TargetVbs)) {
    Write-Error "Could not find: $TargetVbs"
    exit 1
}

$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = "$env:WINDIR\System32\wscript.exe"
$Shortcut.Arguments = "`"$TargetVbs`""
$Shortcut.WorkingDirectory = $Root
$Shortcut.Description = "Dynamic Repair - fix a Part of a dynamic-profile chapter, with an editable reading"
if (Test-Path $IconPath) {
    $Shortcut.IconLocation = $IconPath
}
$Shortcut.Save()

Write-Host "Shortcut created at: $ShortcutPath"
Write-Host "Now right-click it and choose 'Pin to taskbar' (or drag it onto the taskbar)."
