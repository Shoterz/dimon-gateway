param(
    [switch]$Check
)

if (-not $env:DIMON_WHISPER_CPP_MODEL_PATH) {
    Write-Error "DIMON_WHISPER_CPP_MODEL_PATH is required"
    exit 1
}
if (-not $env:DIMON_WHISPER_CPP_BINARY_PATH) {
    Write-Error "DIMON_WHISPER_CPP_BINARY_PATH is required"
    exit 1
}
if (-not (Test-Path -Path $env:DIMON_WHISPER_CPP_MODEL_PATH -PathType Leaf)) {
    Write-Error "WHISPER_ARTIFACT_NOT_FOUND"
    exit 1
}
if (-not (Test-Path -Path $env:DIMON_WHISPER_CPP_BINARY_PATH -PathType Leaf)) {
    Write-Error "WHISPER_RUNTIME_NOT_EXECUTABLE"
    exit 1
}

if ($Check) {
    Write-Output "whisper_cpp: READY"
    exit 0
}

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $repoRoot
uv run python "$repoRoot\services\whisper_cpp\server.py" `
    --binary $env:DIMON_WHISPER_CPP_BINARY_PATH `
    --model $env:DIMON_WHISPER_CPP_MODEL_PATH `
    --host 127.0.0.1 `
    --port 8006
