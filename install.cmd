@echo off
setlocal
title Dimon Gateway - setup
rem Double-click to install or repair. Bypass applies only to this run, so no
rem system-wide PowerShell policy change is needed.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows\install.ps1" %*
set "DIMON_EXIT=%ERRORLEVEL%"
echo.
if not "%DIMON_EXIT%"=="0" echo Setup did not finish. Read the message above, then run install.cmd again - completed steps are kept.
pause
exit /b %DIMON_EXIT%
