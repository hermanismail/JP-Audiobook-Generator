# Create-Shortcut.ps1
# One-time helper: creates a "Chapter Repair.lnk" shortcut on your Desktop,
# pointing to the silent launcher (Launch-Repair-Silent.vbs).
#
# Usage: run this ONCE, either by:
#   - Right-clicking this file -> "Run with PowerShell", or
#   - From a PowerShell prompt:
#       powershell -ExecutionPolicy Bypass -File "C:\JP-Audiobook-Generator\chapter-repair\Create-Shortcut.ps1"
#
# The icon is teal, deliberately unlike the generator (violet), the
# onboarder and the audition tool (amber), so all four are distinguishable
# at a glance on the taskbar.

$Root = "C:\JP-Audiobook-Generator\chapter-repair"
$TargetVbs = Join-Path $Root "Launch-Repair-Silent.vbs"
$IconPath = Join-Path $Root "repair_icon.ico"
$DesktopPath = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $DesktopPath "Chapter Repair.lnk"

if (-not (Test-Path $TargetVbs)) {
    Write-Error "Could not find: $TargetVbs"
    exit 1
}

$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = "$env:WINDIR\System32\wscript.exe"
$Shortcut.Arguments = "`"$TargetVbs`""
$Shortcut.WorkingDirectory = $Root
$Shortcut.Description = "Chapter Repair - replace a hallucinated chunk in a published chapter"
if (Test-Path $IconPath) {
    $Shortcut.IconLocation = $IconPath
}
$Shortcut.Save()

Write-Host "Shortcut created at: $ShortcutPath"
Write-Host "Now right-click it and choose 'Pin to taskbar' (or drag it onto the taskbar)."
