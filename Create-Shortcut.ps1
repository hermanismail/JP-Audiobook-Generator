# Create-Shortcut.ps1
# One-time helper: creates a "JP Audiobook Generator.lnk" shortcut on your
# Desktop, pointing to the silent launcher (Launch-Settings-Silent.vbs).
#
# Usage: run this ONCE, either by:
#   - Right-clicking this file -> "Run with PowerShell", or
#   - From a PowerShell prompt:
#       powershell -ExecutionPolicy Bypass -File "C:\JP-Audiobook-Generator\Create-Shortcut.ps1"
#
# After it runs, a shortcut appears on your Desktop. Pin THAT shortcut to
# the taskbar (right-click it -> Pin to taskbar, or drag it onto the taskbar).

$TargetVbs = "C:\JP-Audiobook-Generator\Launch-Settings-Silent.vbs"
$DesktopPath = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $DesktopPath "JP Audiobook Generator.lnk"

if (-not (Test-Path $TargetVbs)) {
    Write-Error "Could not find: $TargetVbs"
    exit 1
}

$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = "$env:WINDIR\System32\wscript.exe"
$Shortcut.Arguments = "`"$TargetVbs`""
$Shortcut.WorkingDirectory = "C:\JP-Audiobook-Generator"
$Shortcut.Description = "JP Audiobook Generator - Settings"
$Shortcut.IconLocation = "C:\JP-Audiobook-Generator\audiobook_icon.ico"
$Shortcut.Save()

Write-Host "Shortcut created at: $ShortcutPath"
Write-Host "Now right-click it and choose 'Pin to taskbar' (or drag it onto the taskbar)."
