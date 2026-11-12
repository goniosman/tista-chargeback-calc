@echo off
chcp 65001 >nul
title Tista One-Time Login
color 0e

echo ======================================================================
echo                 Tista One-Time Login Helper
echo ======================================================================
echo.

set PYTHON_BIN=
where py >nul 2>&1 && set PYTHON_BIN=py -3
if "%PYTHON_BIN%"=="" (
    where python >nul 2>&1 && set PYTHON_BIN=python
)

if "%PYTHON_BIN%"=="" (
    color 0c
    echo [ERROR] Python not found. Please install Python first.
    pause
    exit /b 1
)

echo [*] Opening Tista Login page...
%PYTHON_BIN% login_tista.py

echo.
echo [✓] Done! You can now run START_CALCULATOR.bat.
pause
