@echo off
echo Stopping AXIOM server on port 8000...
set FOUND=0
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do (
    set FOUND=1
    echo Stopping PID %%p...
    taskkill /PID %%p /F
)
if "%FOUND%"=="0" (
    echo Nothing was running on port 8000.
    pause
    exit /b
)
timeout /t 1 /nobreak >nul
netstat -ano | findstr ":8000" | findstr "LISTENING" >nul
if %errorlevel%==0 (
    echo.
    echo COULD NOT STOP IT - Access denied.
    echo The server is running with Administrator rights, so it needs to be closed the same way.
    echo Right-click this file (stop_server.bat) and choose "Run as administrator", then try again.
) else (
    echo Done. Server stopped.
)
pause