# Kept for older instructions; start.cmd / scripts\windows\start.ps1 replace it.
param([switch]$NoBrowser)
& (Join-Path $PSScriptRoot "start.ps1") -NoBrowser:$NoBrowser
exit $LASTEXITCODE
