param(
    [switch]$Check
)

. (Join-Path $PSScriptRoot "..\..\scripts\windows\Import-DimonEnv.ps1")

function Fail([string]$Reason) {
    [Console]::Error.WriteLine("qwen: $Reason")
    exit 1
}

if (-not $env:DIMON_QWEN_MODEL_PATH) { Fail "DIMON_QWEN_MODEL_PATH is required (run install.cmd and choose Local AI)" }
if (-not $env:DIMON_LLAMA_SERVER_PATH) { Fail "DIMON_LLAMA_SERVER_PATH is required (run install.cmd and choose Local AI)" }
if (-not (Test-Path -LiteralPath $env:DIMON_QWEN_MODEL_PATH -PathType Leaf)) { Fail "QWEN_ARTIFACT_NOT_FOUND" }
if (-not (Test-Path -LiteralPath $env:DIMON_LLAMA_SERVER_PATH -PathType Leaf)) { Fail "QWEN_RUNTIME_NOT_EXECUTABLE" }

# The port comes from DIMON_QWEN_API_BASE so the app and the server agree.
$port = 8080
if ($env:DIMON_QWEN_API_BASE) {
    try { $port = ([Uri]$env:DIMON_QWEN_API_BASE).Port } catch { Fail "DIMON_QWEN_API_BASE is not a valid URL" }
}

if ($Check) {
    Write-Output "qwen: READY"
    exit 0
}

& $env:DIMON_LLAMA_SERVER_PATH `
    --model $env:DIMON_QWEN_MODEL_PATH `
    --host 127.0.0.1 `
    --port $port `
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
