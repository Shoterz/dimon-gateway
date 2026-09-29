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
    # Release asset names embed the build tag (llama-b11255-bin-win-vulkan-x64.zip)
    # and the repos' "latest" releases carry no Windows assets, so assets are
    # resolved by pattern against the most recent releases instead of a fixed URL.
    [string]$LlamaCppAssetPattern = '^llama-b\d+-bin-win-vulkan-x64\.zip$',
    # whisper.cpp publishes no Windows Vulkan build; this is the CPU build.
    [string]$WhisperCppAssetPattern = '^whisper-bin-x64\.zip$'
)

$ErrorActionPreference = "Stop"

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

function Find-ReleaseAsset($Repo, $Pattern) {
    $releases = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases?per_page=10" `
        -Headers @{ "User-Agent" = "dimon-setup-local-host" }
    foreach ($release in $releases) {
        $asset = $release.assets | Where-Object { $_.name -match $Pattern } | Select-Object -First 1
        if ($asset) {
            return $asset.browser_download_url
        }
    }
    throw "No $Repo release asset matches $Pattern"
}

function Get-AndExtract($Repo, $Pattern, $Name) {
    $url = Find-ReleaseAsset $Repo $Pattern
    $zipPath = Join-Path $InstallDir "$Name.zip"
    $destDir = Join-Path $InstallDir $Name
    # Write-Host, not Write-Output: pipeline output would become part of the
    # function's return value.
    Write-Host "Downloading $Name from $url ..."
    Invoke-WebRequest -Uri $url -OutFile $zipPath
    Expand-Archive -Path $zipPath -DestinationPath $destDir -Force
    Remove-Item $zipPath
    return $destDir
}

function Find-Executable($Dir, $Name) {
    $exe = Get-ChildItem -Path $Dir -Filter $Name -Recurse -File | Select-Object -First 1
    if (-not $exe) {
        throw "$Name not found under $Dir"
    }
    return $exe.FullName
}

$llamaDir = Get-AndExtract "ggml-org/llama.cpp" $LlamaCppAssetPattern "llama-cpp-vulkan"
$whisperDir = Get-AndExtract "ggml-org/whisper.cpp" $WhisperCppAssetPattern "whisper-cpp"

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

$llamaServer = Find-Executable $llamaDir "llama-server.exe"
$whisperCli = Find-Executable $whisperDir "whisper-cli.exe"

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
Write-Output "  services\kokoro\run.ps1   (needs the DIMON_KOKORO_* values; see services\kokoro\README.md)"
# dimon-dev forces fixture mode unless the shell sets DIMON_INFERENCE_MODE;
# dimon-serve honours the value from .env.
Write-Output "  uv run dimon-serve"
