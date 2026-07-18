# Регистрирует автозапуск AXIOM-сервера при входе в Windows через Планировщик задач.
# Задача «AXIOM Server» с триггером «при входе в систему» → запускает start_axiom_hidden.vbs
# (скрыто). Register-ScheduledTask корректно переваривает путь с пробелами и кириллицей —
# поэтому регистрируем через PowerShell, а не через schtasks с ручным экранированием кавычек.
$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$vbs = Join-Path $dir "start_axiom_hidden.vbs"

if (-not (Test-Path $vbs)) {
    Write-Host "[Ошибка] не найден $vbs — запускай скрипт из папки axiom." -ForegroundColor Red
    Read-Host "Enter для выхода"; exit 1
}

$action  = New-ScheduledTaskAction -Execute "wscript.exe" -Argument ('"{0}"' -f $vbs)
$trigger = New-ScheduledTaskTrigger -AtLogOn
# StartWhenAvailable — если ПК был выключен в момент запланированного входа, запустит при первой возможности.
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
# RunLevel Limited — серверу НЕ нужны права админа (порт 8000 не привилегированный).
$principal = New-ScheduledTaskPrincipal -UserId ("{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME) -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName "AXIOM Server" -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null

Write-Host ""
Write-Host "[OK] Автозапуск установлен. Сервер стартует при входе в Windows." -ForegroundColor Green
Write-Host "Проверить / запустить прямо сейчас, не перезагружаясь:"
Write-Host '    schtasks /run /tn "AXIOM Server"'
Write-Host "Убрать автозапуск: uninstall_autostart.bat"
Write-Host ""
Read-Host "Enter для выхода"
