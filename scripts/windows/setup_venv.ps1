# Creates the Python virtual environment and installs pinned dependencies.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $root
python --version
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
Write-Host "`nDone. Next: scripts\windows\create_database.ps1" -ForegroundColor Green
