@echo off
title Tista Delay Data Fetcher
cd /d "%~dp0"
echo ========================================================
echo Tista Delay Data Fetcher
echo ========================================================
py -3.11 tista_delay_fetch.py %*
echo.
pause

