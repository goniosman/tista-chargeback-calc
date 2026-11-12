@echo off
chcp 65001 >nul
title Stop Tista Calculator Server
color 0c

echo ======================================================================
echo              Stopping Tista Calculator Server...
echo ======================================================================
echo.

set FOUND=0
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8765" ^| findstr "LISTENING"') do (
    echo Killing process PID %%a ...
    taskkill /f /pid %%a >nul 2>&1
    set FOUND=1
)

if "%FOUND%"=="1" (
    echo [✓] Tista Calculator Server has been stopped successfully.
) else (
    echo [i] No running server found on port 8765.
)

echo.
pause

