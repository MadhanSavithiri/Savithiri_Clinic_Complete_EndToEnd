# Creates the PostgreSQL user + database for the clinic. Passwords are typed hidden.
param([string]$PgBin = "C:\Program Files\PostgreSQL\16\bin")
$ErrorActionPreference = "Stop"
$psql = Join-Path $PgBin "psql.exe"
if (-not (Test-Path $psql)) { throw "psql.exe not found at $psql. Re-run with -PgBin 'path\to\PostgreSQL\bin'" }

function Read-Plain($prompt) {
    $s = Read-Host $prompt -AsSecureString
    $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
    try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
}

$adminPw = Read-Plain "Password of the PostgreSQL 'postgres' user (set during PostgreSQL install)"
$appPw   = Read-Plain "NEW password for the clinic database user (12-64 chars: letters, digits, _ . ! + = @ -)"
if ($appPw -notmatch '^[A-Za-z0-9_.!+=@-]{12,64}$') { throw "Password must be 12-64 characters using only letters, digits and _ . ! + = @ -" }

$env:PGPASSWORD = $adminPw
try {
    "CREATE ROLE clinic_app LOGIN PASSWORD '$appPw' CREATEDB;" | & $psql -U postgres -h 127.0.0.1 -v ON_ERROR_STOP=1 -f -
    "CREATE DATABASE clinic_db OWNER clinic_app ENCODING 'UTF8';" | & $psql -U postgres -h 127.0.0.1 -v ON_ERROR_STOP=1 -f -
    Write-Host "`nDatabase 'clinic_db' and user 'clinic_app' created." -ForegroundColor Green
    Write-Host "Next: scripts\windows\init_env.ps1 (use the SAME password you just chose)."
} finally {
    Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
}
