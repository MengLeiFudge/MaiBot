[CmdletBinding()]
param(
    [switch]$ForceRestart,
    [switch]$SkipInstall,
    [ValidateRange(15, 600)]
    [int]$ReadyTimeoutSeconds = 120
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RuntimeDirectory = Join-Path $ProjectRoot ".runtime"
$PidFile = Join-Path $RuntimeDirectory "maibot.pid"
$LogDirectory = Join-Path $ProjectRoot "logs"
$StdoutLog = Join-Path $LogDirectory "yelin-launcher.out.log"
$StderrLog = Join-Path $LogDirectory "yelin-launcher.err.log"
$WebUiUrl = "http://127.0.0.1:8003/"

function Test-WebUiReady {
    try {
        $response = Invoke-WebRequest -Uri $WebUiUrl -UseBasicParsing -TimeoutSec 2
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 500
    }
    catch {
        return $false
    }
}

function Get-OwnedProcess {
    if (-not (Test-Path -LiteralPath $PidFile)) {
        return $null
    }

    $savedPid = [int](Get-Content -LiteralPath $PidFile -Raw)
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $savedPid" -ErrorAction SilentlyContinue
    if ($null -eq $process -or $process.CommandLine -notlike "*$ProjectRoot*") {
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        return $null
    }
    return $process
}

function Wait-WebUiReady {
    param([System.Diagnostics.Process]$StartedProcess)

    $deadline = [DateTime]::UtcNow.AddSeconds($ReadyTimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-WebUiReady) {
            return
        }
        if ($null -ne $StartedProcess -and $StartedProcess.HasExited) {
            throw "MaiBot exited early with code $($StartedProcess.ExitCode). Logs: $StdoutLog ; $StderrLog"
        }
        Start-Sleep -Seconds 1
    }
    throw "MaiBot WebUI 8003 did not become ready within $ReadyTimeoutSeconds seconds. Logs: $StdoutLog ; $StderrLog"
}

$uv = Get-Command uv -ErrorAction SilentlyContinue
if ($null -eq $uv) {
    throw "uv was not found. Install uv and ensure it is available on PATH."
}

New-Item -ItemType Directory -Path $RuntimeDirectory, $LogDirectory -Force | Out-Null
$ownedProcess = Get-OwnedProcess

if ($ForceRestart -and $null -ne $ownedProcess) {
    & taskkill.exe /PID $ownedProcess.ProcessId /T /F | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to stop this project's MaiBot process $($ownedProcess.ProcessId)."
    }
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    $ownedProcess = $null
}

if ($null -ne $ownedProcess) {
    Wait-WebUiReady
    Write-Host "Yelin MaiBot is already running. PID: $($ownedProcess.ProcessId). WebUI: $WebUiUrl. Logs: $StdoutLog ; $StderrLog"
    return
}

if (Test-WebUiReady) {
    throw "WebUI port 8003 is owned by a process outside this launcher; refusing to take it over."
}

Push-Location $ProjectRoot
try {
    if ($SkipInstall) {
        if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot ".venv"))) {
            throw "The project .venv must exist when -SkipInstall is used."
        }
        $uvRunArguments = @("run", "--no-sync", "python")
    }
    else {
        & $uv.Source sync --frozen
        if ($LASTEXITCODE -ne 0) {
            throw "uv sync --frozen failed."
        }
        $uvRunArguments = @("run", "--no-sync", "python")
    }

    & $uv.Source @uvRunArguments "scripts/enforce_chat_only.py"
    if ($LASTEXITCODE -ne 0) {
        throw "The chat-only policy failed; MaiBot was not started."
    }

    $botPathArgument = '"' + (Join-Path $ProjectRoot "bot.py") + '"'
    $startedProcess = Start-Process -FilePath $uv.Source `
        -ArgumentList @("run", "--no-sync", "python", $botPathArgument) `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $StdoutLog `
        -RedirectStandardError $StderrLog `
        -PassThru
    Set-Content -LiteralPath $PidFile -Value $startedProcess.Id -Encoding ascii
    Wait-WebUiReady -StartedProcess $startedProcess
    Write-Host "Yelin MaiBot is ready. PID: $($startedProcess.Id). WebUI: $WebUiUrl. Logs: $StdoutLog ; $StderrLog"
}
catch {
    if (Test-Path -LiteralPath $PidFile) {
        $currentProcess = Get-OwnedProcess
        if ($null -eq $currentProcess) {
            Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        }
    }
    throw
}
finally {
    Pop-Location
}
