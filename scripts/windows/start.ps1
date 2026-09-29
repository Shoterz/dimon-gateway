<#
.SYNOPSIS
  Starts Dimon Gateway in the mode chosen at install time and opens the browser.

.DESCRIPTION
  Demo mode starts only the web app. Local mode also starts Qwen (llama.cpp),
  whisper.cpp and Kokoro. Every process runs hidden with its output in
  runtime\logs. If a default port is taken by another program, a free port is
  chosen and passed to the app, so nothing needs editing. Use -Wait (as
  start.cmd does) to keep a status window that stops everything on Q.
#>

param(
    [switch]$NoBrowser,
    [switch]$Wait,
    [int]$TimeoutSeconds = 480
)

. (Join-Path $PSScriptRoot "common.ps1")
trap {
    if (Get-Command Write-UnexpectedError -ErrorAction SilentlyContinue) { Write-UnexpectedError $_ } else { Write-Host "    [x] $($_.Exception.Message)" -ForegroundColor Red }
    & (Join-Path $PSScriptRoot "stop.ps1") -Quiet
    exit 1
}

$uv = Find-Uv
if (-not $uv -or -not (Test-Path -LiteralPath $DimonEnvFile)) {
    Stop-WithError "Dimon is not installed on this PC yet." "Double-click install.cmd first."
}
Use-UvOnPath $uv

Write-Host ""
Write-Host "  Dimon Gateway" -ForegroundColor White
& (Join-Path $PSScriptRoot "stop.ps1") -Quiet   # a previous run must not hold the ports

. (Join-Path $PSScriptRoot "Import-DimonEnv.ps1")
$mode = if ($env:DIMON_INFERENCE_MODE) { $env:DIMON_INFERENCE_MODE } else { "fixture" }
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
[void][IO.Directory]::CreateDirectory($DimonLogDir)
[void][IO.Directory]::CreateDirectory((Split-Path -Parent $DimonStateFile))

$services = @()
if ($mode -eq "local") {
    $services = @(
        @{ Name = "kokoro"; Label = "Speech output (Kokoro)"; UrlVar = "DIMON_KOKORO_URL"; Default = "http://127.0.0.1:3001" },
        @{ Name = "whisper_cpp"; Label = "Speech recognition (whisper.cpp)"; UrlVar = "DIMON_WHISPER_CPP_URL"; Default = "ws://127.0.0.1:8006/v1/stream" },
        @{ Name = "qwen"; Label = "Language model (Qwen)"; UrlVar = "DIMON_QWEN_API_BASE"; Default = "http://127.0.0.1:8080/v1" }
    )
    Write-Step "Checking local models"
    foreach ($service in $services) {
        $output = & (Join-Path $DimonRepo "services\$($service.Name)\run.ps1") -Check 2>&1
        if ($LASTEXITCODE -ne 0) {
            Stop-WithError "$($service.Label) is not set up: $output" "Run install.cmd and choose Local AI."
        }
    }
    Write-Ok "Models and runtimes found"
}

Write-Step "Reserving ports"
foreach ($service in $services) {
    $url = [Environment]::GetEnvironmentVariable($service.UrlVar)
    if (-not $url) { $url = $service.Default }
    $port = Get-UrlPort $url 0
    if (-not (Test-PortFree $port)) {
        $free = Find-FreePort ($port + 1)
        Write-Caution "Port $port is used by $(Get-PortOwner $port); $($service.Label) will use $free."
        $url = Set-UrlPort $url $free
        $port = $free
    }
    [Environment]::SetEnvironmentVariable($service.UrlVar, $url)
    $service.Port = $port
}
$appPort = if ($env:DIMON_PORT) { [int]$env:DIMON_PORT } else { 8890 }
if (-not (Test-PortFree $appPort)) {
    $free = Find-FreePort ($appPort + 1)
    Write-Caution "Port $appPort is used by $(Get-PortOwner $appPort); the app will use $free."
    $appPort = $free
}
$env:DIMON_PORT = "$appPort"
$env:DIMON_ALLOWED_ORIGINS = "http://127.0.0.1:$appPort,http://localhost:$appPort"
Write-Ok "Ports ready"

function Start-Hidden([string]$Name, [string]$File, [string]$Arguments) {
    # Start-Process reads these paths as wildcards, so escape [ and ].
    $out = [Management.Automation.WildcardPattern]::Escape((Join-Path $DimonLogDir "$Name.log"))
    $err = [Management.Automation.WildcardPattern]::Escape((Join-Path $DimonLogDir "$Name.err.log"))
    $cwd = [Management.Automation.WildcardPattern]::Escape($DimonRepo)
    return Start-Process -FilePath $File -ArgumentList $Arguments -WorkingDirectory $cwd `
        -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err -PassThru
}

function Show-LogTail([string]$Name) {
    foreach ($suffix in ".err.log", ".log") {
        $log = Join-Path $DimonLogDir "$Name$suffix"
        if ((Test-Path -LiteralPath $log) -and (Get-Item -LiteralPath $log).Length -gt 0) {
            Write-Host "    --- last lines of runtime\logs\$Name$suffix ---" -ForegroundColor DarkGray
            Get-Content -LiteralPath $log -Tail 12 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
        }
    }
}

function Wait-Healthy([string]$Name, [string]$Label, [string]$Url, $Process) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $announced = $false
    while ((Get-Date) -lt $deadline) {
        try {
            if ((Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200) {
                if ($announced) { Write-Host "" }
                Write-Ok "$Label ready"
                return
            }
        } catch {}
        if ($Process.HasExited) {
            if ($announced) { Write-Host "" }
            Show-LogTail $Name
            Stop-WithError "$Label stopped while starting." "See the log above; running install.cmd again usually repairs a broken install."
        }
        if (-not $announced) { Write-Host -NoNewline "    Starting $Label" ; $announced = $true } else { Write-Host -NoNewline "." }
        Start-Sleep -Seconds 2
    }
    Write-Host ""
    Show-LogTail $Name
    Stop-WithError "$Label did not become ready within $TimeoutSeconds seconds." "A slow first start is normal on CPU-only PCs; try start.cmd again."
}

Write-Step "Starting Dimon ($(if ($mode -eq 'local') { 'Local AI' } elseif ($mode -eq 'fixture') { 'demo mode' } else { $mode }))"
$started = @()
foreach ($service in $services) {
    $script = Join-Path $DimonRepo "services\$($service.Name)\run.ps1"
    $service.Process = Start-Hidden $service.Name "powershell.exe" "-NoProfile -ExecutionPolicy Bypass -File `"$script`""
    $started += @{ name = $service.Name; pid = $service.Process.Id }
}
$app = Start-Hidden "app" $uv "run --locked dimon-serve"
$started += @{ name = "app"; pid = $app.Id }
ConvertTo-Json @($started) | Set-Content -LiteralPath $DimonStateFile -Encoding Ascii

foreach ($service in $services) {
    Wait-Healthy $service.Name $service.Label "http://127.0.0.1:$($service.Port)/health" $service.Process
}
$qwen = $services | Where-Object { $_.Name -eq "qwen" }
if ($qwen) {
    # Warm the model so the first real request is not the slow one.
    try {
        $body = '{"messages":[{"role":"user","content":"hi"}],"max_tokens":1}'
        Invoke-WebRequest -Uri "http://127.0.0.1:$($qwen.Port)/v1/chat/completions" -Method Post -Body $body `
            -ContentType "application/json" -UseBasicParsing -TimeoutSec 180 | Out-Null
        Write-Ok "Language model warmed up"
    } catch {
        Write-Caution "Model warm-up skipped; the first request may take longer."
    }
}
Wait-Healthy "app" "Web app" "http://127.0.0.1:$appPort/static/app.css" $app

$address = "http://localhost:$appPort"
Write-Host ""
Write-Host "  Dimon is running at $address" -ForegroundColor Green
if ($mode -eq "fixture") {
    Write-Host "  Demo mode: try the example prompts on the page. Run install.cmd to switch to Local AI."
}
if (-not $NoBrowser) { Start-Process $address }

if ($Wait) {
    Write-Host ""
    Write-Host "  Keep this window open while you use Dimon. Press Q to stop it." -ForegroundColor White
    Write-Host "  (Logs: runtime\logs. Closing this window leaves Dimon running; use stop.cmd.)"
    $watched = @($services | ForEach-Object { @{ Name = $_.Name; Label = $_.Label; Process = $_.Process } }) + @(@{ Name = "app"; Label = "Web app"; Process = $app })
    while ($true) {
        if ([Console]::KeyAvailable) {
            $key = [Console]::ReadKey($true)
            if ($key.Key -eq "Q") { break }
        }
        foreach ($entry in $watched) {
            if ($entry.Process.HasExited -and -not $entry.Reported) {
                Write-Caution "$($entry.Label) stopped unexpectedly."
                Show-LogTail $entry.Name
                $entry.Reported = $true
            }
        }
        Start-Sleep -Milliseconds 500
    }
    Write-Step "Stopping Dimon"
    & (Join-Path $PSScriptRoot "stop.ps1")
}
