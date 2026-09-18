# Create-Shortcut.ps1
# One-time helper: creates a "Book Profiler.lnk" shortcut on your Desktop,
# pointing to the silent launcher (Launch-Profiler-Silent.vbs).
#
# Usage: run this ONCE, either by:
#   - Right-clicking this file -> "Run with PowerShell", or
#   - From a PowerShell prompt:
#       powershell -ExecutionPolicy Bypass -File "C:\JP-Audiobook-Generator\book-profiler\Create-Shortcut.ps1"
#
# After it runs, a shortcut appears on your Desktop. Pin THAT shortcut to the
# taskbar (right-click it -> Pin to taskbar, or drag it onto the taskbar).
#
# The icon is teal, unlike the generator's, the onboarder's, the audition
# tool's and chapter-repair's, so all of them tell apart on the taskbar.

$Root = "C:\JP-Audiobook-Generator\book-profiler"
$TargetVbs = Join-Path $Root "Launch-Profiler-Silent.vbs"
$IconPath = Join-Path $Root "profiler_icon.ico"
$DesktopPath = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $DesktopPath "Book Profiler.lnk"

if (-not (Test-Path $TargetVbs)) {
    Write-Error "Could not find: $TargetVbs"
    exit 1
}

$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = "$env:WINDIR\System32\wscript.exe"
$Shortcut.Arguments = "`"$TargetVbs`""
$Shortcut.WorkingDirectory = $Root
$Shortcut.Description = "Book Profiler - measure a seiyuu against a book and build a dynamic-mode profile"
if (Test-Path $IconPath) {
    $Shortcut.IconLocation = $IconPath
}
$Shortcut.Save()

Write-Host "Shortcut created at: $ShortcutPath"
Write-Host "Now right-click it and choose 'Pin to taskbar' (or drag it onto the taskbar)."
