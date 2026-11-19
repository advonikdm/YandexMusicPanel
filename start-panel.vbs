' Silent launcher for the Yandex Music corner panel (no console window).
' v1.1: starts the watcher ("--watch") - the panel appears when Yandex Music starts.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(fso.GetAbsolutePathName(WScript.ScriptFullName))
sh.CurrentDirectory = dir
exe = "C:\Users\User\AppData\Local\Programs\Python\Python312\pythonw.exe"
If Not fso.FileExists(exe) Then exe = "pythonw.exe"
py = dir & "\panel.py"
sh.Run Chr(34) & exe & Chr(34) & " " & Chr(34) & py & Chr(34) & " " & Chr(34) & "--watch" & Chr(34), 0, False
