# Panel online: keeps the local panel (127.0.0.1:8765) and a reverse SSH tunnel to the server running,
# restarting either one when it stops. The panel in turn keeps the queue worker running (panel.watchdog_s).
# Caddy on the server serves the panel over HTTPS (scripts/server_setup.sh, docs/panel-deploy.md).
#   powershell -ExecutionPolicy Bypass -File scripts\panel_online.ps1
# Start at Windows logon: scripts\autostart.ps1. Log: output\logs\panel_online.log
# Kept ASCII-only on purpose: Windows PowerShell 5.1 misreads UTF-8 without BOM.
param(
    [string]$Server = "195.133.56.48",
    [string]$User = "shortstunnel",
    [int]$Port = 8765,
    [int]$CheckSeconds = 15
)

Set-Location (Split-Path $PSScriptRoot -Parent)
$root = Get-Location
$key = Join-Path $root ".ssh\shorts_tunnel"
$ssh = "$env:WINDIR\System32\OpenSSH\ssh.exe"
$logs = Join-Path $root "output\logs"
New-Item -ItemType Directory -Force $logs | Out-Null
Start-Transcript -Path (Join-Path $logs "panel_online.log") -Append | Out-Null

if (-not (Test-Path $key)) {
    Write-Host "No tunnel key $key - see docs/panel-deploy.md" -ForegroundColor Red
    exit 1
}

function Start-Panel {
    if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { return }
    Write-Host "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') starting panel on 127.0.0.1:$Port"
    Start-Process -FilePath (Join-Path $root "venv\Scripts\python.exe") -ArgumentList "-m", "panel", "--port", $Port `
        -WorkingDirectory $root -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logs "panel.log") `
        -RedirectStandardError (Join-Path $logs "panel.err.log")
    Start-Sleep -Seconds 3
}

$tunnel = $null
while ($true) {
    Start-Panel
    if ($null -eq $tunnel -or $tunnel.HasExited) {
        if ($null -ne $tunnel) {
            Write-Host "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') tunnel dropped (code $($tunnel.ExitCode)), reconnecting" -ForegroundColor Yellow
        }
        Write-Host "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') tunnel -> $User@$Server (127.0.0.1:$Port)"
        $tunnel = Start-Process -FilePath $ssh -NoNewWindow -PassThru -ArgumentList @(
            "-i", "`"$key`"", "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
            "-R", "127.0.0.1:${Port}:127.0.0.1:${Port}", "$User@$Server")
        $null = $tunnel.Handle   # keep the handle so ExitCode is readable after the process exits
    }
    Start-Sleep -Seconds $CheckSeconds
}
