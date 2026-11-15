@echo off
title Tista Chargeback Calculator
cd /d "%~dp0"

echo ======================================================================
echo          Tista Chargeback Calculator Launcher
echo ======================================================================
echo.

:: Detect Python - Prefer py -3.11 with full Playwright environment
set "PYTHON_EXE="
py -3.11 --version >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_EXE=py -3.11"
) else (
    where py >nul 2>&1
    if not errorlevel 1 (
        set "PYTHON_EXE=py -3"
    ) else (
        where python >nul 2>&1
        if not errorlevel 1 (
            set "PYTHON_EXE=python"
        )
    )
)

if "%PYTHON_EXE%"=="" (
    echo [ERROR] Python is not installed or not in PATH!
    echo Please install Python from: https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

echo [✓] Using Python: %PYTHON_EXE%
echo [*] Launching Chargeback Calculator...
echo.

%PYTHON_EXE% Chargeback_Calculator.py

if errorlevel 1 (
    echo.
    echo [!] Application exited with an error.
    pause
)
