<#
.SYNOPSIS
  Downloads vendor-agnostic (Vulkan) llama.cpp and whisper.cpp Windows
  binaries and prints the .env values needed to run Dimon's local inference
  mode on this machine, regardless of GPU vendor.

.DESCRIPTION
  Does not modify apps/dimon_live/.env directly, so an existing configured
  file is never silently overwritten. Does not touch services/gpu_host.
  Model weight files (the Qwen .gguf, the whisper.cpp ggml-*.bin) are not
  downloaded by this script -- there is no single canonical source path
  across developer machines, so that stays a manual step.
#>

param(
    [string]$InstallDir = "$HOME\dimon-local-host",
    [string]$LlamaCppRelease = "https://github.com/ggml-org/llama.cpp/releases/latest/download/llama-bin-win-vulkan-x64.zip",
    [string]$WhisperCppRelease = "https://github.com/ggml-org/whisper.cpp/releases/latest/download/whisper-bin-x64.zip"
)

$ErrorActionPreference = "Stop"

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

function Get-AndExtract($Url, $Name) {
    $zipPath = Join-Path $InstallDir "$Name.zip"
    $destDir = Join-Path $InstallDir $Name
    Write-Output "Downloading $Name from $Url ..."
    Invoke-WebRequest -Uri $Url -OutFile $zipPath
    Expand-Archive -Path $zipPath -DestinationPath $destDir -Force
    Remove-Item $zipPath
    return $destDir
}

$llamaDir = Get-AndExtract $LlamaCppRelease "llama-cpp-vulkan"
$whisperDir = Get-AndExtract $WhisperCppRelease "whisper-cpp-vulkan"

Write-Output ""
Write-Output "=== GPU / Vulkan check ==="
$vulkaninfo = Get-Command vulkaninfo -ErrorAction SilentlyContinue
if ($vulkaninfo) {
    $summary = & vulkaninfo --summary 2>&1
    if ($summary -match "deviceName") {
        Write-Output "Vulkan driver found -- GPU acceleration should work."
    } else {
        Write-Output "vulkaninfo ran but reported no device -- Qwen/STT will fall back to CPU (slower, but functional)."
    }
} else {
    Write-Output "vulkaninfo not found on PATH -- can't confirm GPU driver presence. Qwen/STT will still run (CPU fallback) if no Vulkan device is available at launch time."
}

$llamaServer = Join-Path $llamaDir "llama-server.exe"
$whisperCli = Join-Path $whisperDir "whisper-cli.exe"

Write-Output ""
Write-Output "=== Add these to apps/dimon_live/.env ==="
Write-Output "DIMON_INFERENCE_MODE=local"
Write-Output "DIMON_STT_BACKEND=whisper_cpp"
Write-Output "DIMON_LLAMA_SERVER_PATH=$llamaServer"
Write-Output "DIMON_QWEN_MODEL_PATH=<path to your downloaded Qwen .gguf>"
Write-Output "DIMON_WHISPER_CPP_BINARY_PATH=$whisperCli"
Write-Output "DIMON_WHISPER_CPP_MODEL_PATH=<path to your downloaded ggml-*.bin whisper model>"
Write-Output ""
Write-Output "Then start each service in its own terminal:"
Write-Output "  services\qwen\run.ps1"
Write-Output "  services\whisper_cpp\run.ps1"
Write-Output "  uv run dimon-dev"
