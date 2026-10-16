@echo off
setlocal
title Dimon Gateway - stop
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows\stop.ps1"
timeout /t 3 >nul
