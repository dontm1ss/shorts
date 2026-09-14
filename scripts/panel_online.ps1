# Panel online: starts the local panel (127.0.0.1:8765) if it is not running, then keeps a
# reverse SSH tunnel to the server open, reconnecting forever. Caddy on the server serves it
# over HTTPS (scripts/server_setup.sh, docs/panel-deploy.md).
#   powershell -ExecutionPolicy Bypass -File scripts\panel_online.ps1
# Kept ASCII-only on purpose: Windows PowerShell 5.1 misreads UTF-8 without BOM.
param(
    [string]$Server = "195.133.56.48",
    [string]$User = "shortstunnel",
    [int]$Port = 8765
)

Set-Location (Split-Path $PSScriptRoot -Parent)
$root = Get-Location
$key = Join-Path $root ".ssh\shorts_tunnel"
$ssh = "$env:WINDIR\System32\OpenSSH\ssh.exe"

if (-not (Test-Path $key)) {
    Write-Host "No tunnel key $key - see docs/panel-deploy.md" -ForegroundColor Red
    exit 1
}

$listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if (-not $listening) {
    Write-Host "Starting panel on 127.0.0.1:$Port"
    Start-Process -FilePath (Join-Path $root "venv\Scripts\python.exe") -ArgumentList "-m", "panel" `
        -WorkingDirectory $root -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $root "output\logs\panel.log") `
        -RedirectStandardError (Join-Path $root "output\logs\panel.err.log")
    Start-Sleep -Seconds 3
}

while ($true) {
    Write-Host "$(Get-Date -Format 'HH:mm:ss') tunnel -> $User@$Server (127.0.0.1:$Port)"
    & $ssh -i $key -N -o BatchMode=yes -o ExitOnForwardFailure=yes `
        -o ServerAliveInterval=30 -o ServerAliveCountMax=3 `
        -R "127.0.0.1:${Port}:127.0.0.1:${Port}" "$User@$Server"
    Write-Host "Tunnel dropped (code $LASTEXITCODE), reconnecting in 10 s" -ForegroundColor Yellow
    Start-Sleep -Seconds 10
}
