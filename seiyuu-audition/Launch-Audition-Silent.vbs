' Launch-Audition-Silent.vbs
' Silently launches Run-Audition.ps1 (no PowerShell console window flash).
' This is the file the taskbar shortcut actually points to.

Set objShell = CreateObject("WScript.Shell")
scriptPath = "C:\JP-Audiobook-Generator\seiyuu-audition\Run-Audition.ps1"
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & scriptPath & """"
objShell.Run cmd, 0, False
