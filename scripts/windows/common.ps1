# Shared helpers for the Windows scripts. Dot-source it:
#   . (Join-Path $PSScriptRoot "common.ps1")
# Written for Windows PowerShell 5.1, the version every Windows 10/11 machine
# has, and kept ASCII-only because 5.1 misreads UTF-8 files without a BOM.

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # the 5.1 progress bar makes downloads crawl
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch {}

# -LiteralPath everywhere: a folder such as "Downloads\dimon [1]" must not be
# read as a wildcard pattern.
$DimonRepo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
$DimonEnvFile = Join-Path $DimonRepo "apps\dimon_live\.env"
$DimonEnvExample = Join-Path $DimonRepo ".env.example"
$DimonRuntime = Join-Path $DimonRepo "runtime"
$DimonStateFile = Join-Path $DimonRuntime "windows\processes.json"
$DimonLogDir = Join-Path $DimonRuntime "logs"

function Get-DimonHome {
    if ($env:DIMON_HOME) { return $env:DIMON_HOME }
    return (Join-Path $env:LOCALAPPDATA "DimonGateway")
}

function Write-Step([string]$Message) { Write-Host ""; Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Ok([string]$Message) { Write-Host "    [ok] $Message" -ForegroundColor Green }
function Write-Note([string]$Message) { Write-Host "    $Message" }
function Write-Caution([string]$Message) { Write-Host "    [!] $Message" -ForegroundColor Yellow }
function Stop-WithError([string]$Message, [string]$Fix) {
    Write-Host ""
    Write-Host "    [x] $Message" -ForegroundColor Red
    if ($Fix) { Write-Host "        Fix: $Fix" -ForegroundColor Red }
    $global:DimonErrorShown = $true
    throw $Message
}

# Entry scripts use this in a trap so failures read as one clear message,
# never as a PowerShell stack trace.
function Write-UnexpectedError($ErrorRecord) {
    if ($global:DimonErrorShown) { return }
    Write-Host ""
    Write-Host "    [x] $($ErrorRecord.Exception.Message)" -ForegroundColor Red
    Write-Host "        (at $($ErrorRecord.InvocationInfo.ScriptName | Split-Path -Leaf):$($ErrorRecord.InvocationInfo.ScriptLineNumber))" -ForegroundColor DarkGray
}

# ---------------------------------------------------------------- .env files

function Read-DotEnv([string]$Path = $DimonEnvFile) {
    $values = @{}
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        foreach ($line in [IO.File]::ReadAllLines($Path)) {
            if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') {
                $values[$Matches[1]] = $Matches[2].Trim('"', "'")
            }
        }
    }
    return $values
}

# Update keys in place (keeping comments and every other value) and append
# keys the file does not have yet.
function Set-DotEnvValues([hashtable]$Values, [string]$Path = $DimonEnvFile) {
    $lines = New-Object System.Collections.Generic.List[string]
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        foreach ($line in [IO.File]::ReadAllLines($Path)) { $lines.Add($line) }
    }
    $pending = @{}
    foreach ($key in $Values.Keys) { $pending[$key] = $true }
    for ($index = 0; $index -lt $lines.Count; $index++) {
        if ($lines[$index] -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=') {
            $key = $Matches[1]
            if ($Values.ContainsKey($key)) {
                $lines[$index] = "$key=$($Values[$key])"
                $pending.Remove($key)
            }
        }
    }
    foreach ($key in ($pending.Keys | Sort-Object)) { $lines.Add("$key=$($Values[$key])") }
    [IO.File]::WriteAllLines($Path, $lines.ToArray(), (New-Object System.Text.UTF8Encoding($false)))
}

function Initialize-DotEnv {
    if (-not (Test-Path -LiteralPath $DimonEnvFile -PathType Leaf)) {
        Copy-Item -LiteralPath $DimonEnvExample -Destination $DimonEnvFile
        return $true
    }
    return $false
}

# ------------------------------------------------------------------- tooling

function Find-Uv {
    $command = Get-Command uv -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    foreach ($candidate in @(
        (Join-Path $HOME ".local\bin\uv.exe"),
        (Join-Path $env:USERPROFILE ".cargo\bin\uv.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\uv\uv.exe")
    )) {
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    return $null
}

function Use-UvOnPath([string]$UvExe) {
    $directory = Split-Path -Parent $UvExe
    if (($env:PATH -split ';') -notcontains $directory) { $env:PATH = "$directory;$env:PATH" }
}

# ----------------------------------------------------------------- downloads

function Test-FileHash([string]$Path, [string]$Sha256) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    return ((Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() -eq $Sha256)
}

function Format-Size([long]$Bytes) {
    if ($Bytes -ge 1GB) { return "{0:N1} GB" -f ($Bytes / 1GB) }
    return "{0:N0} MB" -f ($Bytes / 1MB)
}

# Resumable, retried download verified by SHA-256. Skips work already done.
function Get-VerifiedFile([hashtable]$Artifact, [string]$Destination, [string]$Label) {
    if (Test-FileHash $Destination $Artifact.Sha256) {
        Write-Ok "$Label already downloaded and verified"
        return
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Destination) | Out-Null
    $partial = "$Destination.partial"
    Write-Note "Downloading $Label ($(Format-Size $Artifact.Bytes))..."
    $curl = Join-Path $env:WINDIR "System32\curl.exe"
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        if (Test-Path -LiteralPath $curl) {
            & $curl -L --fail --retry 5 --retry-delay 3 --connect-timeout 30 -C - --progress-bar -o $partial $Artifact.Url
            $ok = ($LASTEXITCODE -eq 0)
        } else {
            try {
                Invoke-WebRequest -Uri $Artifact.Url -OutFile $partial -UseBasicParsing
                $ok = $true
            } catch { $ok = $false }
        }
        if ($ok -and (Test-FileHash $partial $Artifact.Sha256)) {
            Move-Item -LiteralPath $partial -Destination $Destination -Force
            Write-Ok "$Label verified (SHA-256)"
            return
        }
        if ($ok) {
            # Complete but wrong: a resumed file cannot fix that, start over.
            Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
        }
        Write-Caution "Download attempt $attempt for $Label did not verify; retrying..."
    }
    Stop-WithError "Could not download $Label" "Check your internet connection (or proxy) and run install.cmd again; finished downloads are kept."
}

function Expand-VerifiedZip([hashtable]$Artifact, [string]$InstallRoot, [string]$Label) {
    $zip = Join-Path $InstallRoot ("downloads\" + [IO.Path]::GetFileName(([Uri]$Artifact.Url).AbsolutePath))
    Get-VerifiedFile $Artifact $zip $Label
    $target = Join-Path $InstallRoot $Artifact.Dir
    $marker = Join-Path $target ".sha256"
    if ((Test-Path -LiteralPath $marker) -and ((Get-Content -LiteralPath $marker -Raw).Trim() -eq $Artifact.Sha256)) {
        return $target
    }
    if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
    Expand-Archive -LiteralPath $zip -DestinationPath $target -Force
    Get-ChildItem -LiteralPath $target -Recurse -File | Unblock-File
    Set-Content -LiteralPath $marker -Value $Artifact.Sha256 -Encoding Ascii
    return $target
}

# --------------------------------------------------------------------- ports

# Binding is the only reliable test: it also catches listeners that
# Get-NetTCPConnection cannot see (for example WSL's mirrored network).
function Test-PortFree([int]$Port) {
    $listener = $null
    try {
        $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $Port)
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        if ($listener) { $listener.Stop() }
    }
}

function Get-PortOwner([int]$Port) {
    try {
        $connection = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop | Select-Object -First 1
        $process = Get-Process -Id $connection.OwningProcess -ErrorAction Stop
        return $process.ProcessName
    } catch {
        return "another program (possibly WSL or a VM)"
    }
}

function Find-FreePort([int]$Preferred) {
    for ($port = $Preferred; $port -lt $Preferred + 100; $port++) {
        if (Test-PortFree $port) { return $port }
    }
    Stop-WithError "No free port near $Preferred" "Close other local servers and try again."
}

function Get-UrlPort([string]$Url, [int]$Default) {
    try { $uri = [Uri]$Url; if ($uri.Port -gt 0) { return $uri.Port } } catch {}
    return $Default
}

function Set-UrlPort([string]$Url, [int]$Port) {
    $builder = New-Object System.UriBuilder($Url)
    $builder.Port = $Port
    return $builder.Uri.AbsoluteUri.TrimEnd('/')
}
