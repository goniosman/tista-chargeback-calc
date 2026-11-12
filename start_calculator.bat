@echo off
chcp 65001 >nul
title Tista Chargeback Calculator Launcher
color 0b

echo ======================================================================
echo          Tista Chargeback Calculator - Automatic Launcher
echo ======================================================================
echo.

:: 1. Detect Python
set PYTHON_BIN=
where py >nul 2>&1 && set PYTHON_BIN=py -3
if "%PYTHON_BIN%"=="" (
    where python >nul 2>&1 && set PYTHON_BIN=python
)

if "%PYTHON_BIN%"=="" (
    color 0c
    echo [ERROR] Python is not installed or not added to PATH!
    echo.
    echo দয়া করে Python ডাউনলোড করে ইন্সটল করুন:
    echo 1. ভিজিট করুন: https://www.python.org/downloads/
    echo 2. ইন্সটলেশনের সময় অবশ্যই "Add Python to PATH" চেকবক্সে টিক দিন!
    echo.
    echo Press any key to open Python download page in your browser...
    pause >nul
    start https://www.python.org/downloads/
    exit /b 1
)

echo [✓] Python detected: %PYTHON_BIN%
echo.

:: 2. Check and install Playwright
echo [*] Checking dependencies...
%PYTHON_BIN% -c "import playwright" >nul 2>&1
if errorlevel 1 (
    echo [*] Installing required library (Playwright)...
    %PYTHON_BIN% -m pip install -r requirements.txt
    if errorlevel 1 (
        color 0c
        echo [ERROR] Failed to install requirements. Please check your internet connection.
        pause
        exit /b 1
    )
)
echo [✓] Python dependencies OK!

:: 3. Check Playwright Chromium Browser
echo [*] Checking Playwright Chromium browser...
%PYTHON_BIN% -m playwright install chromium >nul 2>&1
echo [✓] Browser OK!
echo.

:: 4. Check Login session
set PROFILE_EXISTS=0
if exist "%USERPROFILE%\.tista-profile" set PROFILE_EXISTS=1
if exist ".tista-profile" set PROFILE_EXISTS=1

if "%PROFILE_EXISTS%"=="0" (
    echo ======================================================================
    echo  [!] First Time Setup: Tista Login Required
    echo  একটি ব্রাউজার উইন্ডো ওপেন হবে।
    echo  আপনার Tista ইমেইল ও পাসওয়ার্ড দিয়ে একবার লগইন করে নিন।
    echo  (লগইন সেশন স্বয়ংক্রিয়ভাবে সেভ হয়ে থাকবে।)
    echo ======================================================================
    echo.
    %PYTHON_BIN% login_tista.py
    echo.
)

:: 5. Launch Calculator in Standalone Desktop Window Mode (No browser tabs, no URL bar)
echo ======================================================================
echo  [✓] Starting Chargeback Calculator as Standalone Desktop App...
echo  (ব্রাউজার ছাড়া সরাসরি আলাদা ডেস্কটপ অ্যাপ উইন্ডোতে ওপেন হচ্ছে...)
echo ======================================================================
echo.

set BROWSER_APP=
if exist "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" set BROWSER_APP="C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
if "%BROWSER_APP%"=="" if exist "C:\Program Files\Microsoft\Edge\Application\msedge.exe" set BROWSER_APP="C:\Program Files\Microsoft\Edge\Application\msedge.exe"
if "%BROWSER_APP%"=="" if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" set BROWSER_APP="C:\Program Files\Google\Chrome\Application\chrome.exe"
if "%BROWSER_APP%"=="" if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" set BROWSER_APP="C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"

if not "%BROWSER_APP%"=="" (
    start "" %BROWSER_APP% --app="http://localhost:8765/Chargeback%%20Calculator.html" --window-size=1360,900
) else (
    start "" "%PYTHON_BIN%" -c "import time, webbrowser; time.sleep(1.2); webbrowser.open('http://localhost:8765/')"
)

:: Run the server
%PYTHON_BIN% tista_delay_fetch.py --serve

pause
