<#
.SYNOPSIS
  One-step Windows installer for Dimon Gateway. Safe to run again at any time.

.DESCRIPTION
  1. Installs uv (Python toolchain) if missing and syncs the locked environment.
  2. Creates apps\dimon_live\.env from the template (never overwrites your values).
  3. Picks a mode for this PC:
       demo  - fixture inference: no downloads, works on any Windows PC.
       local - real Qwen, whisper.cpp and Kokoro on this PC (~9 GB download,
               16 GB RAM recommended). Uses the GPU through Vulkan when present
               (NVIDIA, AMD or Intel) and falls back to CPU otherwise.
  4. For local mode: installs the Visual C++ runtime if needed, downloads
     pinned runtimes and models (SHA-256 verified, resumable), builds the
     Kokoro speech environment, and writes every path into .env.
  5. Adds "Dimon Gateway" shortcuts to the Desktop and Start Menu.

.EXAMPLE
  install.cmd                 # guided
  install.cmd -Mode demo -Yes # unattended demo install
#>

param(
    [ValidateSet("auto", "demo", "local")]
    [string]$Mode = "auto",
    [switch]$Yes,
    [switch]$NoShortcuts
)

. (Join-Path $PSScriptRoot "common.ps1")
trap {
    if (Get-Command Write-UnexpectedError -ErrorAction SilentlyContinue) { Write-UnexpectedError $_ } else { Write-Host "    [x] $($_.Exception.Message)" -ForegroundColor Red }
    exit 1
}
$artifacts = Import-PowerShellDataFile (Join-Path $PSScriptRoot "artifacts.psd1")
$installRoot = Get-DimonHome

Write-Host ""
Write-Host "  Dimon Gateway - Windows setup" -ForegroundColor White
Write-Host "  Synthetic-ledger research demo. Nothing here touches real bank accounts."
Write-Host "  Repository: $DimonRepo"

# ------------------------------------------------------------ system check
Write-Step "Checking this PC"
$os = [Environment]::OSVersion.Version
if ($os.Major -lt 10) {
    Stop-WithError "Windows 10 or 11 is required (found $os)." "Upgrade Windows, or use the demo on another PC."
}
$arch = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
$ramGb = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
New-Item -ItemType Directory -Force -Path $installRoot | Out-Null
$freeGb = [math]::Round((New-Object IO.DriveInfo ([IO.Path]::GetPathRoot($installRoot))).AvailableFreeSpace / 1GB)
$gpus = @(Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name })
$hasVulkan = Test-Path -LiteralPath (Join-Path $env:WINDIR "System32\vulkan-1.dll")
Write-Ok "Windows $($os.Major).$($os.Build), $arch, $ramGb GB RAM, $freeGb GB free on $([IO.Path]::GetPathRoot($installRoot))"
Write-Ok ("Graphics: " + ($(if ($gpus) { $gpus -join ", " } else { "none detected" })) + $(if ($hasVulkan) { " (Vulkan available)" } else { " (no Vulkan driver: CPU only)" }))

# -------------------------------------------------------------- pick mode
$localBlockers = @()
if ($arch -ne "AMD64") { $localBlockers += "local models need a 64-bit x86 PC (this is $arch)" }
if ($ramGb -lt 12) { $localBlockers += "local models need at least 12 GB RAM (this PC has $ramGb GB)" }
if ($freeGb -lt 12) { $localBlockers += "local models need about 12 GB free disk space ($freeGb GB free)" }

if ($Mode -eq "auto") {
    $existing = Read-DotEnv
    if ($existing["DIMON_INFERENCE_MODE"] -eq "local" -and -not $localBlockers) {
        $Mode = "local"
    } elseif ($localBlockers) {
        $Mode = "demo"
    } elseif ($Yes) {
        $Mode = "demo"
    } else {
        Write-Host ""
        Write-Host "  How do you want to run Dimon?" -ForegroundColor White
        Write-Host "    [1] Demo mode  - ready in about a minute, no model downloads, scripted AI responses."
        Write-Host "    [2] Local AI   - real speech and language models on this PC (~9 GB download)."
        $choice = Read-Host "  Choose 1 or 2 (default 1)"
        $Mode = if ($choice -eq "2") { "local" } else { "demo" }
    }
}
if ($Mode -eq "local" -and $localBlockers) {
    foreach ($blocker in $localBlockers) { Write-Caution $blocker }
    if (-not $Yes) {
        $answer = Read-Host "  This PC is below the local-mode requirements. Continue anyway? (y/N)"
        if ($answer -notmatch '^(y|yes)$') { $Mode = "demo" }
    }
}
Write-Ok "Mode: $Mode"

# ---------------------------------------------------------------- uv + app
Write-Step "Installing the Python toolchain"
$uv = Find-Uv
if (-not $uv) {
    Write-Note "Installing uv (per-user, no administrator rights needed)..."
    $installer = Invoke-RestMethod -Uri "https://astral.sh/uv/install.ps1" -UseBasicParsing
    Invoke-Expression $installer | Out-Null
    $uv = Find-Uv
    if (-not $uv) { Stop-WithError "uv did not install." "Install it from https://docs.astral.sh/uv/ and run install.cmd again." }
}
Use-UvOnPath $uv
Write-Ok "uv $((& $uv --version) -replace '^uv ', '')"

Push-Location $DimonRepo
try {
    Write-Note "Syncing the locked environment (downloads Python 3.12 on first run)..."
    & $uv sync --locked --quiet
    if ($LASTEXITCODE -ne 0) { Stop-WithError "Dependency sync failed." "Check your internet connection and run install.cmd again." }
} finally {
    Pop-Location
}
Write-Ok "Application environment ready"

# Files extracted from a downloaded zip carry the "from the internet" mark,
# which makes Windows prompt on every script; clear it once.
Get-ChildItem -LiteralPath $DimonRepo -Recurse -File -Include *.ps1, *.cmd, *.psd1 -ErrorAction SilentlyContinue | Unblock-File

Write-Step "Configuring"
if (Initialize-DotEnv) { Write-Ok "Created apps\dimon_live\.env from the template" } else { Write-Ok "Keeping your existing apps\dimon_live\.env" }
$settings = @{
    DIMON_INFERENCE_MODE = $(if ($Mode -eq "local") { "local" } else { "fixture" })
    DIMON_ALLOWED_ORIGINS = "http://127.0.0.1:8890,http://localhost:8890"
}

# ------------------------------------------------------------- local mode
if ($Mode -eq "local") {
    Write-Step "Checking the Visual C++ runtime (needed by the model runtimes)"
    $vc = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X64" -ErrorAction SilentlyContinue
    $vcCurrent = $vc -and $vc.Installed -eq 1 -and $vc.Major -eq 14 -and $vc.Minor -ge 40 -and (Test-Path -LiteralPath (Join-Path $env:WINDIR "System32\vcruntime140_1.dll"))
    if ($vcCurrent) {
        Write-Ok "Visual C++ runtime 14.$($vc.Minor) present"
    } else {
        Write-Note "Installing the Microsoft Visual C++ runtime (Windows will ask for permission)..."
        $redist = Join-Path $installRoot "downloads\vc_redist.x64.exe"
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $redist) | Out-Null
        Invoke-WebRequest -Uri "https://aka.ms/vs/17/release/vc_redist.x64.exe" -OutFile $redist -UseBasicParsing
        $signature = Get-AuthenticodeSignature -LiteralPath $redist
        if ($signature.Status -ne "Valid" -or $signature.SignerCertificate.Subject -notmatch "Microsoft Corporation") {
            Stop-WithError "The Visual C++ installer is not signed by Microsoft." "Install it manually from https://aka.ms/vs/17/release/vc_redist.x64.exe"
        }
        $process = Start-Process -FilePath $redist -ArgumentList "/install", "/quiet", "/norestart" -Verb RunAs -Wait -PassThru
        if ($process.ExitCode -notin 0, 1638, 3010) {
            Stop-WithError "Visual C++ runtime install failed (code $($process.ExitCode))." "Install it manually from https://aka.ms/vs/17/release/vc_redist.x64.exe"
        }
        Write-Ok "Visual C++ runtime installed"
    }

    Write-Step "Downloading model runtimes into $installRoot"
    $llamaArtifact = if ($hasVulkan) { $artifacts.Runtimes.LlamaVulkan } else { $artifacts.Runtimes.LlamaCpu }
    $llamaDir = Expand-VerifiedZip $llamaArtifact $installRoot $(if ($hasVulkan) { "llama.cpp (Vulkan GPU build)" } else { "llama.cpp (CPU build)" })
    $whisperDir = Expand-VerifiedZip $artifacts.Runtimes.WhisperCpu $installRoot "whisper.cpp"
    $llamaServer = (Get-ChildItem -LiteralPath $llamaDir -Recurse -Filter "llama-server.exe" | Select-Object -First 1).FullName
    $whisperCli = (Get-ChildItem -LiteralPath $whisperDir -Recurse -Filter "whisper-cli.exe" | Select-Object -First 1).FullName
    if (-not $llamaServer -or -not $whisperCli) { Stop-WithError "Runtime archives are missing executables." "Delete $installRoot\runtimes and run install.cmd again." }
    Write-Ok "Runtimes ready"

    Write-Step "Downloading models (about 7 GB; interrupted downloads resume)"
    $paths = @{}
    foreach ($name in "Qwen", "Whisper", "KokoroModel", "KokoroConfig", "KokoroVoice") {
        $artifact = $artifacts.Models[$name]
        $destination = Join-Path $installRoot $artifact.File
        Get-VerifiedFile $artifact $destination $name
        $paths[$name] = $destination
    }

    Write-Step "Building the Kokoro speech environment (CPU PyTorch, about 1 GB)"
    $kokoroVenv = Join-Path $installRoot "kokoro-venv"
    $kokoroPython = Join-Path $kokoroVenv "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $kokoroPython)) {
        & $uv venv --python 3.12 --quiet $kokoroVenv
        if ($LASTEXITCODE -ne 0) { Stop-WithError "Could not create the Kokoro environment." "Run install.cmd again." }
    }
    & $uv pip install --python $kokoroPython --quiet --index-strategy unsafe-best-match `
        --extra-index-url https://download.pytorch.org/whl/cpu @($artifacts.KokoroPackages)
    if ($LASTEXITCODE -ne 0) { Stop-WithError "Kokoro packages failed to install." "Check your internet connection and run install.cmd again." }
    Write-Ok "Kokoro environment ready"

    $settings["DIMON_STT_BACKEND"] = "whisper_cpp"
    $settings["DIMON_LLAMA_SERVER_PATH"] = $llamaServer
    $settings["DIMON_QWEN_MODEL_PATH"] = $paths["Qwen"]
    $settings["DIMON_WHISPER_CPP_BINARY_PATH"] = $whisperCli
    $settings["DIMON_WHISPER_CPP_MODEL_PATH"] = $paths["Whisper"]
    $settings["DIMON_KOKORO_MODEL_PATH"] = $paths["KokoroModel"]
    $settings["DIMON_KOKORO_CONFIG_PATH"] = $paths["KokoroConfig"]
    $settings["DIMON_KOKORO_VOICE_PATH"] = $paths["KokoroVoice"]
    $settings["DIMON_KOKORO_PYTHON"] = $kokoroPython
}

Set-DotEnvValues $settings
Write-Ok "Settings written to apps\dimon_live\.env"

if ($Mode -eq "local") {
    Write-Step "Verifying the local services"
    foreach ($service in "qwen", "whisper_cpp", "kokoro") {
        & (Join-Path $DimonRepo "services\$service\run.ps1") -Check | Out-Null
        if ($LASTEXITCODE -ne 0) { Stop-WithError "$service failed its preflight check." "Run install.cmd again." }
        Write-Ok "$service ready"
    }
}

# --------------------------------------------------------------- shortcuts
if (-not $NoShortcuts) {
    Write-Step "Adding shortcuts"
    $shell = New-Object -ComObject WScript.Shell
    $startMenu = Join-Path ([Environment]::GetFolderPath("Programs")) "Dimon Gateway"
    New-Item -ItemType Directory -Force -Path $startMenu | Out-Null
    $links = @(
        @{ Path = (Join-Path ([Environment]::GetFolderPath("Desktop")) "Dimon Gateway.lnk"); Target = "start.cmd"; Description = "Start Dimon Gateway" },
        @{ Path = (Join-Path $startMenu "Dimon Gateway.lnk"); Target = "start.cmd"; Description = "Start Dimon Gateway" },
        @{ Path = (Join-Path $startMenu "Stop Dimon Gateway.lnk"); Target = "stop.cmd"; Description = "Stop Dimon Gateway" }
    )
    foreach ($link in $links) {
        $shortcut = $shell.CreateShortcut($link.Path)
        $shortcut.TargetPath = Join-Path $DimonRepo $link.Target
        $shortcut.WorkingDirectory = $DimonRepo
        $shortcut.Description = $link.Description
        $shortcut.IconLocation = "$env:WINDIR\System32\imageres.dll,109"
        $shortcut.Save()
    }
    Write-Ok "Desktop and Start Menu shortcuts created"
}

Write-Host ""
Write-Host "  Setup complete." -ForegroundColor Green
Write-Host "  Start Dimon with the 'Dimon Gateway' shortcut or start.cmd."
Write-Host "  It opens http://localhost:8890 in your browser."
if ($Mode -eq "demo") {
    Write-Host "  Demo mode uses scripted AI responses. Run install.cmd again and choose Local AI to use real models."
}
