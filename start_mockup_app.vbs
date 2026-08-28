' Launches the D2G Mockup Generator silently (no console window) at logon.
' This file lives in the Windows Startup folder as a shortcut target.
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\Users\PhilHine\Documents\mockup-engine-handoff-2026-08-21"
WshShell.Run """C:\Users\PhilHine\AppData\Local\Python\pythoncore-3.14-64\pythonw.exe"" ""C:\Users\PhilHine\Documents\mockup-engine-handoff-2026-08-21\app.py""", 0, False
