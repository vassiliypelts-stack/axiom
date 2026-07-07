@echo off
chcp 65001 >nul
cd /d "%~dp0..\.."
echo === YGROUP: сбор контактов (человеческий темп, с докачкой) ===
py axiom\scrapers\ygroup.py
echo.
echo Готово. Файл: axiom\scrapers\ygroup_contacts.xlsx
pause
