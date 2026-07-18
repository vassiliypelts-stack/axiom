@echo off
rem Убрать автозапуск AXIOM (удалить задачу из Планировщика). Требует прав администратора.
chcp 65001 >nul
title AXIOM — удаление автозапуска
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-Command','schtasks /delete /tn \"AXIOM Server\" /f; Read-Host \"Enter для выхода\"'"
echo Откроется окно с запросом прав администратора — подтверди его.
timeout /t 4 /nobreak >nul
