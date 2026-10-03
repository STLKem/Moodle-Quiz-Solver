# Build MoodleSolver.exe and place a clean copy in ../Release
# Usage from repo root:
#   powershell -ExecutionPolicy Bypass -File .\packaging\build_exe.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

Write-Host "==> Installing build dependencies..." -ForegroundColor Cyan
python -m pip install -r requirements.txt pyinstaller pywebview | Out-Null

Write-Host "==> Building MoodleSolver (onedir)..." -ForegroundColor Cyan
python -m PyInstaller --noconfirm --clean (Join-Path $PSScriptRoot "MoodleSolver.spec")

$out = Join-Path $root "dist\MoodleSolver"
if (-not (Test-Path (Join-Path $out "MoodleSolver.exe"))) {
  Write-Host "Build failed — exe not found." -ForegroundColor Red
  exit 1
}

Copy-Item -Force "config.example.yaml" (Join-Path $out "config.example.yaml")
if ((Test-Path "config.yaml") -and -not (Test-Path (Join-Path $out "config.yaml"))) {
  Copy-Item -Force "config.yaml" (Join-Path $out "config.yaml")
}

$release = Join-Path $root "Release"
if (Test-Path $release) { Remove-Item -Recurse -Force $release }
New-Item -ItemType Directory -Path $release | Out-Null
Copy-Item -Recurse -Force (Join-Path $out "*") $release

# Drop PyInstaller work folders after packaging
Remove-Item -Recurse -Force (Join-Path $root "build") -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force (Join-Path $root "dist") -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "Ready to run:" -ForegroundColor Green
Write-Host "  $release\MoodleSolver.exe"
