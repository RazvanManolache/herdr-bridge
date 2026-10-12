Set shell = CreateObject("Wscript.Shell")
base = shell.ExpandEnvironmentStrings("%USERPROFILE%\.herdr-bridge-emu")
shell.Run Chr(34) & base & "\runtime\Git\usr\bin\perl.exe" & Chr(34) & " " & Chr(34) & base & "\keeper.pl" & Chr(34), 0, False
shell.Run Chr(34) & "C:\Python314\python.exe" & Chr(34) & " -E -s -B -u " & Chr(34) & base & "\snapshot-poller.py" & Chr(34), 0, False
