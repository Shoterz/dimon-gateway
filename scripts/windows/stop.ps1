<#
.SYNOPSIS
  Stops every Dimon process started by start.ps1 (and any orphans from a
  previous run), including child processes such as llama-server.
#>

param(
    [switch]$Quiet
)

. (Join-Path $PSScriptRoot "common.ps1")
$ErrorActionPreference = "Continue"

function Stop-Tree([int]$ProcessId) {
    if (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) {
        & taskkill.exe /PID $ProcessId /T /F 2>&1 | Out-Null
        return $true
    }
    return $false
}

$stopped = 0
if (Test-Path -LiteralPath $DimonStateFile) {
    try {
        foreach ($entry in (Get-Content -LiteralPath $DimonStateFile -Raw | ConvertFrom-Json)) {
            if (Stop-Tree ([int]$entry.pid)) { $stopped++ }
        }
    } catch {}
    Remove-Item -LiteralPath $DimonStateFile -Force -ErrorAction SilentlyContinue
}

# Orphans: service processes whose command line points into this repository
# or the model folder. The scripts that manage Dimon (and this window's
# ancestors) are never touched.
$roots = @($DimonRepo, (Get-DimonHome)) | ForEach-Object { $_.ToLowerInvariant() }
$protected = @{}
$cursor = $PID
while ($cursor) {
    $protected[$cursor] = $true
    $parent = (Get-CimInstance Win32_Process -Filter "ProcessId = $cursor" -ErrorAction SilentlyContinue).ParentProcessId
    if (-not $parent -or $protected.ContainsKey([int]$parent)) { break }
    $cursor = [int]$parent
}
$serviceNames = "python.exe", "uv.exe", "llama-server.exe", "whisper-cli.exe", "powershell.exe", "pwsh.exe"
foreach ($process in Get-CimInstance Win32_Process -ErrorAction SilentlyContinue) {
    if ($protected.ContainsKey([int]$process.ProcessId) -or $serviceNames -notcontains $process.Name.ToLowerInvariant()) { continue }
    $commandLine = "$($process.CommandLine)".ToLowerInvariant()
    if (-not $commandLine -or $commandLine -match '(install|start|stop)\.(ps1|cmd)') { continue }
    $ours = $false
    foreach ($root in $roots) { if ($commandLine.Contains($root)) { $ours = $true } }
    if ($ours -and $commandLine -match 'dimon-serve|server\.py|run\.ps1|llama-server|whisper-cli') {
        if (Stop-Tree ([int]$process.ProcessId)) { $stopped++ }
    }
}

if (-not $Quiet) {
    if ($stopped) { Write-Ok "Dimon stopped" } else { Write-Ok "Dimon was not running" }
}
