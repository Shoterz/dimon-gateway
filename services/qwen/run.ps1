param(
    [switch]$Check
)

. (Join-Path $PSScriptRoot "..\..\scripts\windows\Import-DimonEnv.ps1")

if (-not $env:DIMON_QWEN_MODEL_PATH) {
    Write-Error "DIMON_QWEN_MODEL_PATH is required"
    exit 1
}
if (-not $env:DIMON_LLAMA_SERVER_PATH) {
    Write-Error "DIMON_LLAMA_SERVER_PATH is required"
    exit 1
}
if (-not (Test-Path -Path $env:DIMON_QWEN_MODEL_PATH -PathType Leaf)) {
    Write-Error "QWEN_ARTIFACT_NOT_FOUND"
    exit 1
}
if (-not (Test-Path -Path $env:DIMON_LLAMA_SERVER_PATH -PathType Leaf)) {
    Write-Error "QWEN_RUNTIME_NOT_EXECUTABLE"
    exit 1
}

if ($Check) {
    Write-Output "qwen: READY"
    exit 0
}

& $env:DIMON_LLAMA_SERVER_PATH `
    --model $env:DIMON_QWEN_MODEL_PATH `
    --host 127.0.0.1 `
    --port 8080 `
    --ctx-size 16384 `
    --n-gpu-layers auto `
    --parallel 1 `
    --flash-attn auto `
    --cache-type-k q4_0 `
    --cache-type-v q4_0 `
    --batch-size 512 `
    --ubatch-size 128 `
    --context-shift

exit $LASTEXITCODE
