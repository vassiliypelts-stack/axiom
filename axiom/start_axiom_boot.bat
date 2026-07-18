@echo off
rem Запуск AXIOM-сервера для АВТОСТАРТА (без pause, вывод — в лог).
rem Ручной запуск — start_server.bat. Этот файл дёргает планировщик задач через
rem start_axiom_hidden.vbs (скрыто, без мелькающей консоли).
chcp 65001 >nul
title AXIOM Server (boot)
cd /d "%~dp0"
set "PYTHONPATH=%~dp0"
set "PYTHONIOENCODING=utf-8"

rem снять старый сервер, если висит на 8000 (перезапуск без дублей)
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do taskkill /PID %%p /F >nul 2>&1

rem настоящий Python (не Store-заглушка) — те же пути, что в start_server.bat
set "PY=C:\Users\vp198\AppData\Local\Python\bin\python.exe"
if not exist "%PY%" set "PY=C:\Users\vp198\AppData\Local\Python\pythoncore-3.14-64\python.exe"
if not exist "%PY%" set "PY=python"

if not exist "data\logs" mkdir "data\logs"
echo ===== %date% %time% старт сервера (boot) ===== >> "data\logs\server_boot.log"
"%PY%" -m web.app >> "data\logs\server_boot.log" 2>&1
