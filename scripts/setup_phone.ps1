# Phone automation setup (Windows): downloads adb (platform-tools) and scrcpy,
# installs uiautomator2 into the venv. Run on the PC the phone is USB-connected to.
#   powershell -ExecutionPolicy Bypass -File scripts\setup_phone.ps1
# Kept ASCII-only on purpose: Windows PowerShell 5.1 misreads UTF-8 without BOM.

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Set-Location (Split-Path $PSScriptRoot -Parent)
$py = "venv\Scripts\python.exe"
$tools = "tools"
$scrVer = "v2.7"

function Step($title, [scriptblock]$cmd) {
    Write-Host "==> $title" -ForegroundColor Cyan
    try {
        $global:LASTEXITCODE = 0
        & $cmd
        if ($LASTEXITCODE -ne 0) { throw "exit code $LASTEXITCODE" }
    } catch {
        Write-Host "FAILED: $title - $($_.Exception.Message)" -ForegroundColor Red
        exit 1
    }
}

if (-not (Test-Path $py)) {
    Write-Host "No venv. Run scripts\setup.ps1 first." -ForegroundColor Red
    exit 1
}

New-Item -ItemType Directory -Force -Path $tools | Out-Null

# --- adb (Google platform-tools) ---
if (-not (Test-Path "$tools\platform-tools\adb.exe")) {
    $zip = "$env:TEMP\platform-tools.zip"
    Step "Download platform-tools (adb)" {
        Invoke-WebRequest -Uri "https://dl.google.com/android/repository/platform-tools-latest-windows.zip" -OutFile $zip
    }
    Step "Extract platform-tools" {
        Expand-Archive -Path $zip -DestinationPath "$env:TEMP\pt-extract" -Force
    }
    if (Test-Path "$tools\platform-tools") { Remove-Item -Recurse -Force "$tools\platform-tools" }
    Move-Item -Path "$env:TEMP\pt-extract\platform-tools" -Destination "$tools\platform-tools"
}

# --- scrcpy (screen mirror for debugging) ---
if (-not (Test-Path "$tools\scrcpy\scrcpy.exe")) {
    $zip = "$env:TEMP\scrcpy.zip"
    Step "Download scrcpy $scrVer" {
        Invoke-WebRequest -Uri "https://github.com/Genymobile/scrcpy/releases/download/$scrVer/scrcpy-win64-$scrVer.zip" -OutFile $zip
    }
    Step "Extract scrcpy" {
        Expand-Archive -Path $zip -DestinationPath "$env:TEMP\scr-extract" -Force
    }
    if (Test-Path "$tools\scrcpy") { Remove-Item -Recurse -Force "$tools\scrcpy" }
    Move-Item -Path "$env:TEMP\scr-extract\scrcpy-win64-$scrVer" -Destination "$tools\scrcpy"
}

Step "uiautomator2 into venv" { & $py -m pip install uiautomator2 }

Write-Host ""
Write-Host "Done. Next steps:" -ForegroundColor Green
Write-Host "  1. On the phone: Settings > About phone > tap Build number 7 times (Developer options)."
Write-Host "  2. Developer options > enable USB debugging; connect the cable."
Write-Host "  3. Allow USB debugging from this PC on the phone screen."
Write-Host "  4. Check:  venv\Scripts\python -m phone devices"
Write-Host "  5. Mirror screen (debugging):  tools\scrcpy\scrcpy.exe"
