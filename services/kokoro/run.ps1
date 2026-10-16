param(
    [switch]$Check
)

. (Join-Path $PSScriptRoot "..\..\scripts\windows\Import-DimonEnv.ps1")

function Fail([string]$Reason) {
    [Console]::Error.WriteLine("kokoro: $Reason")
    exit 1
}

foreach ($name in "DIMON_KOKORO_MODEL_PATH", "DIMON_KOKORO_CONFIG_PATH", "DIMON_KOKORO_VOICE_PATH", "DIMON_KOKORO_PYTHON") {
    if (-not [Environment]::GetEnvironmentVariable($name)) { Fail "$name is required (run install.cmd and choose Local AI)" }
}
if (-not (Test-Path -LiteralPath $env:DIMON_KOKORO_MODEL_PATH -PathType Leaf)) { Fail "KOKORO_ARTIFACT_NOT_FOUND" }
if (-not (Test-Path -LiteralPath $env:DIMON_KOKORO_CONFIG_PATH -PathType Leaf)) { Fail "KOKORO_CONFIG_NOT_FOUND" }
if (-not (Test-Path -LiteralPath $env:DIMON_KOKORO_VOICE_PATH -PathType Leaf)) { Fail "KOKORO_VOICE_NOT_FOUND" }
if (-not (Test-Path -LiteralPath $env:DIMON_KOKORO_PYTHON -PathType Leaf)) { Fail "KOKORO_RUNTIME_NOT_EXECUTABLE" }

# The port comes from DIMON_KOKORO_URL so the app and the server agree.
$port = 3001
if ($env:DIMON_KOKORO_URL) {
    try { $port = ([Uri]$env:DIMON_KOKORO_URL).Port } catch { Fail "DIMON_KOKORO_URL is not a valid URL" }
}

if ($Check) {
    Write-Output "kokoro: READY"
    exit 0
}

# Everything Kokoro needs is installed locally; never reach out to the Hub.
$env:HF_HUB_OFFLINE = "1"
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
& $env:DIMON_KOKORO_PYTHON (Join-Path $repoRoot "services\kokoro\server.py") `
    --model $env:DIMON_KOKORO_MODEL_PATH `
    --config $env:DIMON_KOKORO_CONFIG_PATH `
    --voice $env:DIMON_KOKORO_VOICE_PATH `
    --host 127.0.0.1 `
    --port $port

exit $LASTEXITCODE
