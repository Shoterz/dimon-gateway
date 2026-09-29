<#
.SYNOPSIS
  Starts Dimon's full local-inference stack natively on Windows.

.DESCRIPTION
  Opens Qwen (llama-server, Vulkan), whisper.cpp, and Kokoro in their own
  PowerShell windows, waits for each loopback health route, then starts the
  application with dimon-serve (which honours DIMON_INFERENCE_MODE=local from
  apps/dimon_live/.env) and opens the browser. Close a window to stop that
  service. Run scripts\windows\setup-local-host.ps1 first.
#>

param(
    [int]$HealthTimeoutSeconds = 300,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

. (Join-Path $PSScriptRoot "Import-DimonEnv.ps1")

$services = @(
    @{ Name = "qwen"; Script = "services\qwen\run.ps1"; Health = "http://127.0.0.1:8080/health" },
    @{ Name = "whisper_cpp"; Script = "services\whisper_cpp\run.ps1"; Health = "http://127.0.0.1:8006/health" },
    @{ Name = "kokoro"; Script = "services\kokoro\run.ps1"; Health = "http://127.0.0.1:3001/health" }
)

function Test-Healthy($Url) {
    try {
        return (Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200
    } catch {
        return $false
    }
}

function Start-Window($Title, $Command) {
    Start-Process -FilePath "powershell.exe" -WorkingDirectory $repoRoot -ArgumentList @(
        "-NoExit", "-ExecutionPolicy", "Bypass", "-Command",
        "`$Host.UI.RawUI.WindowTitle = 'dimon: $Title'; $Command"
    ) | Out-Null
}

foreach ($service in $services) {
    & (Join-Path $repoRoot $service.Script) -Check | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "$($service.Name) preflight failed; run scripts\windows\setup-local-host.ps1 and check apps\dimon_live\.env"
    }
}

foreach ($service in $services) {
    if (Test-Healthy $service.Health) {
        Write-Host "$($service.Name): already running"
        continue
    }
    Write-Host "$($service.Name): starting"
    Start-Window $service.Name "& '.\$($service.Script)'"
}

$deadline = (Get-Date).AddSeconds($HealthTimeoutSeconds)
foreach ($service in $services) {
    while (-not (Test-Healthy $service.Health)) {
        if ((Get-Date) -gt $deadline) {
            throw "$($service.Name) did not become healthy at $($service.Health); check its window"
        }
        Start-Sleep -Seconds 2
    }
    Write-Host "$($service.Name): READY"
}

$port = if ($env:DIMON_PORT) { $env:DIMON_PORT } else { "8890" }
# Open localhost, not 127.0.0.1: WebAuthn (Windows Hello) rejects IP-address
# origins. Probe the IPv4 loopback the server binds, since localhost resolves
# to ::1 first and can outlast the short health-check timeout.
$appUrl = "http://localhost:$port"
$probeUrl = "http://127.0.0.1:$port"
if (Test-Healthy "$probeUrl/static/app.css") {
    Write-Host "app: already running"
} else {
    Write-Host "app: starting on $appUrl"
    Start-Window "app" "uv run dimon-serve"
}

$deadline = (Get-Date).AddSeconds(60)
while (-not (Test-Healthy "$probeUrl/static/app.css")) {
    if ((Get-Date) -gt $deadline) {
        throw "the application did not start; check the 'dimon: app' window"
    }
    Start-Sleep -Seconds 1
}
Write-Host "app: READY at $appUrl"
if (-not $NoBrowser) {
    Start-Process $appUrl
}
