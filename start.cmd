@echo off
setlocal
title Dimon Gateway
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows\start.ps1" -Wait %*
if errorlevel 1 (
  echo.
  pause
)
