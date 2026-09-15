# Autostart of the shorts farm at Windows logon: scripts\panel_online.ps1 (panel + tunnel, both restarted
# when they stop); the panel keeps the queue worker running. No admin rights needed: a hidden launcher
# in the user's Startup folder (Task Manager -> Startup shows it as shorts-farm).
#   powershell -ExecutionPolicy Bypass -File scripts\autostart.ps1           # enable
#   powershell -ExecutionPolicy Bypass -File scripts\autostart.ps1 -Remove   # disable
#   powershell -ExecutionPolicy Bypass -File scripts\autostart.ps1 -Start    # enable and start right now
# Kept ASCII-only on purpose: Windows PowerShell 5.1 misreads UTF-8 without BOM.
param(
    [switch]$Remove,
    [switch]$Start,
    [string]$Target = (Join-Path $PSScriptRoot "panel_online.ps1"),
    [string]$Launcher = (Join-Path ([Environment]::GetFolderPath("Startup")) "shorts-farm.vbs")
)

if ($Remove) {
    Remove-Item $Launcher -ErrorAction SilentlyContinue
    Write-Host "Autostart removed: $Launcher"
    exit 0
}

$Target = (Resolve-Path $Target).Path
# VBScript string literal: inner quotes are doubled; window style 0 = hidden, no console window at logon
$command = 'powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $Target + '"'
$vbs = @(
    "' shorts farm: panel + tunnel + queue worker at logon. Disable: scripts\autostart.ps1 -Remove",
    ('CreateObject("WScript.Shell").Run "' + $command.Replace('"', '""') + '", 0, False')
)
Set-Content -Path $Launcher -Value $vbs -Encoding Unicode
Write-Host "Autostart enabled: $Launcher -> $Target"

if ($Start) {
    Start-Process -FilePath "wscript.exe" -ArgumentList "`"$Launcher`""
    Write-Host "Started"
}
