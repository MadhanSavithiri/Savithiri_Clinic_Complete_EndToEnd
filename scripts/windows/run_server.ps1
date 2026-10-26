# Starts the clinic server (Waitress). Leave this window open.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $root
& .\.venv\Scripts\python.exe manage.py migrate --noinput
& .\.venv\Scripts\python.exe manage.py collectstatic --noinput | Out-Null
& .\.venv\Scripts\python.exe serve.py
