@echo off
rem Двойной клик — установить автозапуск AXIOM при входе в Windows.
rem Создание задачи в Планировщике требует прав администратора — запросим их (окно UAC).
chcp 65001 >nul
title AXIOM — установка автозапуска
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','\"%~dp0install_autostart.ps1\"'"
echo Откроется окно с запросом прав администратора — подтверди его.
echo Если окно не появилось, проверь, не заблокирован ли UAC.
timeout /t 4 /nobreak >nul
