param(
    [switch]$Check
)

. (Join-Path $PSScriptRoot "..\..\scripts\windows\Import-DimonEnv.ps1")

function Fail([string]$Reason) {
    [Console]::Error.WriteLine("whisper_cpp: $Reason")
    exit 1
}

if (-not $env:DIMON_WHISPER_CPP_MODEL_PATH) { Fail "DIMON_WHISPER_CPP_MODEL_PATH is required (run install.cmd and choose Local AI)" }
if (-not $env:DIMON_WHISPER_CPP_BINARY_PATH) { Fail "DIMON_WHISPER_CPP_BINARY_PATH is required (run install.cmd and choose Local AI)" }
if (-not (Test-Path -LiteralPath $env:DIMON_WHISPER_CPP_MODEL_PATH -PathType Leaf)) { Fail "WHISPER_ARTIFACT_NOT_FOUND" }
if (-not (Test-Path -LiteralPath $env:DIMON_WHISPER_CPP_BINARY_PATH -PathType Leaf)) { Fail "WHISPER_RUNTIME_NOT_EXECUTABLE" }

# The port comes from DIMON_WHISPER_CPP_URL so the app and the server agree.
$port = 8006
if ($env:DIMON_WHISPER_CPP_URL) {
    try { $port = ([Uri]$env:DIMON_WHISPER_CPP_URL).Port } catch { Fail "DIMON_WHISPER_CPP_URL is not a valid URL" }
}

if ($Check) {
    Write-Output "whisper_cpp: READY"
    exit 0
}

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
Set-Location -LiteralPath $repoRoot
uv run python (Join-Path $repoRoot "services\whisper_cpp\server.py") `
    --binary $env:DIMON_WHISPER_CPP_BINARY_PATH `
    --model $env:DIMON_WHISPER_CPP_MODEL_PATH `
    --host 127.0.0.1 `
    --port $port

exit $LASTEXITCODE
