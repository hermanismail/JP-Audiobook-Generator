' Launch-Settings-Silent.vbs
' Silently launches Run-Settings.ps1 (no PowerShell console window flash).
' This is the file the taskbar shortcut will actually point to.

Set objShell = CreateObject("WScript.Shell")
scriptPath = "C:\JP-Audiobook-Generator\Run-Settings.ps1"
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & scriptPath & """"
objShell.Run cmd, 0, False
