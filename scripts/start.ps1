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
$InstanceName = (Split-Path -Leaf $ProjectRoot) -replace '^maibot-', ''
$StdoutLog = Join-Path $LogDirectory "$InstanceName-launcher.out.log"
$StderrLog = Join-Path $LogDirectory "$InstanceName-launcher.err.log"
$VenvDirectory = Join-Path $ProjectRoot ".venv"
$VenvScripts = Join-Path $VenvDirectory "Scripts"
$PythonExe = Join-Path $VenvScripts "python.exe"
$PolicyPath = Join-Path $ProjectRoot "scripts\enforce_chat_only.py"
$BotPath = Join-Path $ProjectRoot "bot.py"

# 独立实例从自己的配置读取端口；缺少环境时只在允许安装时准备依赖。
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    if ($SkipInstall) {
        throw "The project Python $PythonExe must exist when -SkipInstall is used."
    }
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($null -eq $uv) {
        throw "uv was not found. Install uv and ensure it is available on PATH."
    }
    Push-Location $ProjectRoot
    try {
        & $uv.Source sync --frozen
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
            throw "Failed to prepare the MaiBot environment: $PythonExe"
        }
    }
    finally {
        Pop-Location
    }
}
$startupJson = & $PythonExe (Join-Path $PSScriptRoot "read_startup_config.py")
if ($LASTEXITCODE -ne 0) {
    throw "Failed to read startup identity and WebUI port from $ProjectRoot\config\bot_config.toml."
}
$startup = $startupJson | ConvertFrom-Json
$BotLabel = [string]$startup.nickname
$WebUiPort = [int]$startup.webui_port
$WebUiUrl = "http://127.0.0.1:$WebUiPort/"

function Test-WebUiReady {
    try {
        $response = Invoke-WebRequest -Uri $WebUiUrl -UseBasicParsing -TimeoutSec 2
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 500
    }
    catch {
        return $false
    }
}

<#
.SYNOPSIS
检查本机是否已有进程监听 MaiBot WebUI 端口；查询失败时向调用方传播异常。
#>
function Test-WebUiPortInUse {
    $listeners = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
    return $listeners.Port -contains $WebUiPort
}

function Get-OwnedProcess {
    if (-not (Test-Path -LiteralPath $PidFile)) {
        return $null
    }

    $savedPid = 0
    if (-not [int]::TryParse((Get-Content -LiteralPath $PidFile -Raw).Trim(), [ref]$savedPid)) {
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        return $null
    }

    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $savedPid" -OperationTimeoutSec 5 -ErrorAction SilentlyContinue
    if ($null -eq $process -or
        [string]::IsNullOrWhiteSpace($process.CommandLine) -or
        $process.CommandLine.IndexOf($ProjectRoot, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        return $null
    }
    return $process
}

function Wait-WebUiReady {
    param([System.Diagnostics.Process]$StartedProcess)

    $deadline = [DateTime]::UtcNow.AddSeconds($ReadyTimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($null -ne $StartedProcess -and $StartedProcess.HasExited) {
            throw "MaiBot exited early with code $($StartedProcess.ExitCode). Logs: $StdoutLog ; $StderrLog"
        }
        if (Test-WebUiReady) {
            return
        }
        Start-Sleep -Seconds 1
    }
    throw "MaiBot WebUI $WebUiPort did not become ready within $ReadyTimeoutSeconds seconds. Logs: $StdoutLog ; $StderrLog"
}

$launchTimer = [System.Diagnostics.Stopwatch]::StartNew()
New-Item -ItemType Directory -Path $RuntimeDirectory, $LogDirectory -Force | Out-Null
$ownedProcess = Get-OwnedProcess

if ($ForceRestart -and $null -ne $ownedProcess) {
    $stopTimer = [System.Diagnostics.Stopwatch]::StartNew()
    $taskkillProcess = Start-Process -FilePath (Join-Path $env:SystemRoot "System32\taskkill.exe") `
        -ArgumentList @("/PID", $ownedProcess.ProcessId, "/T", "/F") `
        -WindowStyle Hidden `
        -PassThru
    try {
        [void]$taskkillProcess.Handle
        if (-not $taskkillProcess.WaitForExit(10000)) {
            $taskkillProcess.Kill()
            throw "Timed out while stopping this project's MaiBot process $($ownedProcess.ProcessId)."
        }
        if ($taskkillProcess.ExitCode -ne 0) {
            throw "Failed to stop this project's MaiBot process $($ownedProcess.ProcessId)."
        }
    }
    finally {
        $taskkillProcess.Dispose()
    }

    $releaseDeadline = [DateTime]::UtcNow.AddSeconds(15)
    do {
        $remainingProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $($ownedProcess.ProcessId)" -OperationTimeoutSec 5 -ErrorAction SilentlyContinue
        $portInUse = Test-WebUiPortInUse
        if ($null -eq $remainingProcess -and -not $portInUse) {
            break
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $releaseDeadline)

    if ($null -ne $remainingProcess -or $portInUse) {
        throw "MaiBot process tree or WebUI port $WebUiPort was not released within 15 seconds."
    }
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    $ownedProcess = $null
    Write-Host "Stopped previous $BotLabel MaiBot in $([Math]::Round($stopTimer.Elapsed.TotalSeconds, 1))s."
}

if ($null -ne $ownedProcess) {
    Wait-WebUiReady
    Write-Host "$BotLabel MaiBot is already running. PID: $($ownedProcess.ProcessId). WebUI: $WebUiUrl. Logs: $StdoutLog ; $StderrLog. Elapsed: $([Math]::Round($launchTimer.Elapsed.TotalSeconds, 1))s."
    return
}

if (Test-WebUiPortInUse) {
    throw "WebUI port $WebUiPort is owned by a process outside this launcher; refusing to take it over."
}

Push-Location $ProjectRoot
$startedProcess = $null
try {
    Write-Host "Reusing MaiBot environment: $PythonExe"

    $policyTimer = [System.Diagnostics.Stopwatch]::StartNew()
    & $PythonExe $PolicyPath
    if ($LASTEXITCODE -ne 0) {
        throw "The $BotLabel plugin allowlist policy failed; MaiBot was not started."
    }
    Write-Host "$BotLabel plugin allowlist policy passed in $([Math]::Round($policyTimer.Elapsed.TotalSeconds, 1))s."

    $processEnvironment = @{}
    foreach ($entry in [Environment]::GetEnvironmentVariables().GetEnumerator()) {
        $processEnvironment[[string]$entry.Key] = [string]$entry.Value
    }
    $processEnvironment["VIRTUAL_ENV"] = $VenvDirectory
    $processEnvironment["PATH"] = "$VenvScripts;$($processEnvironment['PATH'])"
    $environmentVariables = @($processEnvironment.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" })

    $cmdPath = Join-Path $env:SystemRoot "System32\cmd.exe"
    $commandLine = '"' + $cmdPath + '" /d /s /c ""' + $PythonExe + '" "' + $BotPath + '" <NUL >"' + $StdoutLog + '" 2>"' + $StderrLog + '""'
    $startupClass = Get-CimClass -ClassName Win32_ProcessStartup -OperationTimeoutSec 5
    $startup = New-CimInstance -CimClass $startupClass -ClientOnly -Property @{
        ShowWindow = [uint16]0
        CreateFlags = [uint32]0x00000010
        EnvironmentVariables = [string[]]$environmentVariables
    }
    $createResult = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine = $commandLine
        CurrentDirectory = $ProjectRoot
        ProcessStartupInformation = $startup
    } -OperationTimeoutSec 10
    if ($createResult.ReturnValue -ne 0 -or $createResult.ProcessId -le 0) {
        throw "Win32_Process.Create failed with return code $($createResult.ReturnValue)."
    }

    Set-Content -LiteralPath $PidFile -Value $createResult.ProcessId -Encoding ascii
    try {
        $startedProcess = [System.Diagnostics.Process]::GetProcessById($createResult.ProcessId)
    }
    catch {
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        throw "MaiBot background process $($createResult.ProcessId) exited before it could be observed. Logs: $StdoutLog ; $StderrLog"
    }

    Write-Host "Started $BotLabel MaiBot background root. PID: $($createResult.ProcessId). Waiting for WebUI..."
    Wait-WebUiReady -StartedProcess $startedProcess
    Write-Host "$BotLabel MaiBot is ready. PID: $($createResult.ProcessId). WebUI: $WebUiUrl. Logs: $StdoutLog ; $StderrLog. Elapsed: $([Math]::Round($launchTimer.Elapsed.TotalSeconds, 1))s."
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
    if ($null -ne $startedProcess) {
        $startedProcess.Dispose()
    }
    Pop-Location
}
