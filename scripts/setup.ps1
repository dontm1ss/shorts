# Environment setup for the current machine's hardware profile (Windows).
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
# Kept ASCII-only on purpose: Windows PowerShell 5.1 misreads UTF-8 without BOM.

Set-Location (Split-Path $PSScriptRoot -Parent)
$py = "venv\Scripts\python.exe"

function Step($title, [scriptblock]$cmd) {
    Write-Host "==> $title" -ForegroundColor Cyan
    & $cmd
    if ($LASTEXITCODE -ne 0) {
        Write-Host "FAILED: $title" -ForegroundColor Red
        exit 1
    }
}

if (-not (Test-Path $py)) {
    Step "Create venv (Python 3.11)" { py -3.11 -m venv venv }
}
Step "pip, setuptools<81, wheel" { & $py -m pip install --upgrade pip "setuptools<81" wheel }

$profileName = & $py src\core\config.py --get profile.name
$index = & $py src\core\config.py --get install.torch_index_url
if ($LASTEXITCODE -ne 0) { exit 1 }
Write-Host "Hardware profile: $profileName"

if ($index) {
    Step "torch 2.6.0 from $index" { & $py -m pip install torch==2.6.0 torchaudio==2.6.0 --index-url $index }
} else {
    Step "torch 2.6.0 from PyPI" { & $py -m pip install torch==2.6.0 torchaudio==2.6.0 }
}
Step "requirements.txt" { & $py -m pip install -r requirements.txt }
Step "project package (editable)" { & $py -m pip install -e . }

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Host "ffmpeg not found in PATH. Install it: winget install Gyan.FFmpeg" -ForegroundColor Yellow
}
Step "Environment checklist" { & $py scripts\check_env.py }
