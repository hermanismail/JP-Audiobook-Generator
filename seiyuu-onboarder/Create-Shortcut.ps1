# Create-Shortcut.ps1
# One-time helper: creates a "Seiyuu Onboarder.lnk" shortcut on your Desktop,
# pointing to the silent launcher (Launch-Onboarder-Silent.vbs).
#
# Usage: run this ONCE, either by:
#   - Right-clicking this file -> "Run with PowerShell", or
#   - From a PowerShell prompt:
#       powershell -ExecutionPolicy Bypass -File "C:\JP-Audiobook-Generator\seiyuu-onboarder\Create-Shortcut.ps1"
#
# After it runs, a shortcut appears on your Desktop. Pin THAT shortcut to the
# taskbar (right-click it -> Pin to taskbar, or drag it onto the taskbar).
#
# The icon is deliberately different from the audiobook generator's, so the
# two tools are distinguishable at a glance on the taskbar.

$Root = "C:\JP-Audiobook-Generator\seiyuu-onboarder"
$TargetVbs = Join-Path $Root "Launch-Onboarder-Silent.vbs"
$IconPath = Join-Path $Root "seiyuu_icon.ico"
$DesktopPath = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $DesktopPath "Seiyuu Onboarder.lnk"

if (-not (Test-Path $TargetVbs)) {
    Write-Error "Could not find: $TargetVbs"
    exit 1
}

$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = "$env:WINDIR\System32\wscript.exe"
$Shortcut.Arguments = "`"$TargetVbs`""
$Shortcut.WorkingDirectory = $Root
$Shortcut.Description = "Seiyuu Onboarder - train a new Irodori-TTS speaker"
if (Test-Path $IconPath) {
    $Shortcut.IconLocation = $IconPath
}
$Shortcut.Save()

Write-Host "Shortcut created at: $ShortcutPath"
Write-Host "Now right-click it and choose 'Pin to taskbar' (or drag it onto the taskbar)."
