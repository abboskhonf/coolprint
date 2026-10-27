@echo off
chcp 65001 >nul
rem Creates / configures the machine-wide Canon logical printers (see create_printers.ps1).
rem Usage: create_printers.bat [-Source "Canon_Long"] [-NoA3] [-NoVerify]
net session >nul 2>&1
if errorlevel 1 (
    echo Run this as administrator: right-click - Run as administrator.
    pause
    exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0create_printers.ps1" %*
echo.
pause
