# Build AI Switch for Windows (PowerShell)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$Version = (Get-Content VERSION -Raw).Trim()
Write-Host "==> Building AI Switch v$Version for windows-amd64"

python -m pip install -q -r requirements.txt "pyinstaller>=6.0"
if (Test-Path build) { Remove-Item -Recurse -Force build }
if (Test-Path dist) { Remove-Item -Recurse -Force dist }

python -m PyInstaller --noconfirm --clean --distpath dist --workpath build/pyinstaller packaging/ai-switch.spec

$OutDir = "dist/packages"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$Asset = "ai-switch-$Version-windows-amd64"
$Stage = Join-Path $OutDir $Asset
if (Test-Path $Stage) { Remove-Item -Recurse -Force $Stage }
New-Item -ItemType Directory -Force -Path $Stage | Out-Null

Copy-Item dist/ai-switch.exe $Stage/
Copy-Item README.md,LICENSE,VERSION $Stage/ -ErrorAction SilentlyContinue
@"
@echo off
cd /d %~dp0
ai-switch.exe
"@ | Set-Content -Path (Join-Path $Stage "start.bat") -Encoding ASCII

$Zip = Join-Path $OutDir "$Asset.zip"
if (Test-Path $Zip) { Remove-Item -Force $Zip }
Compress-Archive -Path $Stage -DestinationPath $Zip
Write-Host "✅ Package ready: $Zip"
Get-Item $Zip | Format-List FullName,Length
