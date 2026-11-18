' Silent launcher for the Yandex Music corner panel (no console window).
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(fso.GetAbsolutePathName(WScript.ScriptFullName))
sh.CurrentDirectory = dir
sh.Run """C:\Users\User\AppData\Local\Programs\Python\Python312\pythonw.exe"" """ & dir & "\panel.py""", 0, False
