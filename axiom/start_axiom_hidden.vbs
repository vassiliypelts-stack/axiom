' Запускает AXIOM-сервер СКРЫТО (без окна консоли). Нужен для автозапуска при входе
' в Windows: планировщик задач дёргает этот .vbs, а он — start_axiom_boot.bat в фоне.
' Так консоль не мелькает при каждом входе в систему.
Set sh = CreateObject("WScript.Shell")
dir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
sh.CurrentDirectory = dir
' 0 = скрытое окно, False = не ждать завершения (сервер работает постоянно)
sh.Run """" & dir & "start_axiom_boot.bat""", 0, False
