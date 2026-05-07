param(
    [int]$Port = 7861,
    [string]$BindHost = "127.0.0.1",
    [switch]$ForceRestart
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-ListeningPid {
    param([int]$TargetPort)
    $line = netstat -ano | Select-String ":$TargetPort " | Select-String "LISTENING" | Select-Object -First 1
    if (-not $line) {
        return $null
    }
    $parts = ($line.ToString() -split "\s+") | Where-Object { $_ -ne "" }
    if ($parts.Count -lt 5) {
        return $null
    }
    return [int]$parts[-1]
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$uiPath = Join-Path $repoRoot "src\web_ui.py"
$outLog = Join-Path $repoRoot "reports\web_ui.out.log"
$errLog = Join-Path $repoRoot "reports\web_ui.err.log"

if (-not (Test-Path $uiPath)) {
    Write-Error "UI script not found: $uiPath"
}

$existingPid = Get-ListeningPid -TargetPort $Port
if ($existingPid) {
    if (-not $ForceRestart) {
        Write-Host "UI already running on http://$BindHost`:$Port (PID $existingPid)."
        Write-Host "Use -ForceRestart to stop and restart."
        exit 0
    }
    try {
        Stop-Process -Id $existingPid -Force
        Start-Sleep -Milliseconds 700
    }
    catch {
        Write-Error "Failed to stop existing PID ${existingPid}: $($_.Exception.Message)"
    }
}

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Error "Python not found in PATH."
}

New-Item -ItemType Directory -Force -Path (Join-Path $repoRoot "reports") | Out-Null

$startInfo = @{
    FilePath = "python"
    ArgumentList = @("-u", "src/web_ui.py")
    WorkingDirectory = $repoRoot
    RedirectStandardOutput = $outLog
    RedirectStandardError = $errLog
    PassThru = $true
    WindowStyle = "Hidden"
}

$proc = Start-Process @startInfo
$newPid = $null
for ($i = 0; $i -lt 20; $i++) {
    if ($proc.HasExited) {
        Write-Host "UI process exited (exit code $($proc.ExitCode))."
        Write-Host "Check logs:"
        Write-Host "  $outLog"
        Write-Host "  $errLog"
        exit 1
    }
    $newPid = Get-ListeningPid -TargetPort $Port
    if ($newPid) {
        break
    }
    Start-Sleep -Seconds 1
}

if (-not $newPid) {
    Write-Host "Process started (PID $($proc.Id)), but port $Port is still not listening after 20s."
    Write-Host "Check logs:"
    Write-Host "  $outLog"
    Write-Host "  $errLog"
    exit 1
}

Write-Host "UI started: http://$BindHost`:$Port (PID $newPid)"
Write-Host "Logs:"
Write-Host "  $outLog"
Write-Host "  $errLog"
