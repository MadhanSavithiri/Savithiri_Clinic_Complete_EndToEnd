# Creates .env with a fresh random secret key. Never overwrites an existing .env.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $root
if (Test-Path .env) { Write-Host ".env already exists - leaving it untouched."; exit 0 }

$bytes = New-Object byte[] 48
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$secret = ([Convert]::ToBase64String($bytes)) -replace '[+/=]', ''

$ip = Read-Host "Server's fixed LAN IP address (e.g. 192.168.1.100)"
if ($ip -notmatch '^\d{1,3}(\.\d{1,3}){3}$') { throw "That does not look like an IPv4 address." }
$s = Read-Host "Clinic database password (the one you chose in create_database.ps1)" -AsSecureString
$b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
$pw = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b)
[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b)
if ($pw -notmatch '^[A-Za-z0-9_.!+=@-]{12,64}$') { throw "Password format not allowed (see create_database.ps1)." }

$lines = Get-Content .env.example | ForEach-Object {
    if     ($_ -like 'DJANGO_SECRET_KEY=*')    { "DJANGO_SECRET_KEY=$secret" }
    elseif ($_ -like 'DJANGO_ALLOWED_HOSTS=*') { "DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,$ip" }
    elseif ($_ -like 'DB_PASSWORD=*')          { "DB_PASSWORD=$pw" }
    else { $_ }
}
[System.IO.File]::WriteAllLines((Join-Path $root ".env"), $lines, (New-Object System.Text.UTF8Encoding $false))
Write-Host ".env created." -ForegroundColor Green
