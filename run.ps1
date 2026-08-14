[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$envFile = Join-Path $PSScriptRoot '.env'
$logDir = Join-Path $PSScriptRoot 'logs'
$watchdogLog = Join-Path $logDir 'watchdog.log'

if (-not (Test-Path -LiteralPath $python)) {
    throw 'Sanal ortam bulunamadi. Once .\setup.ps1 calistirin.'
}
if (-not (Test-Path -LiteralPath $envFile)) {
    throw '.env bulunamadi. Once .\setup.ps1 calistirin ve yeni API anahtarini ekleyin.'
}

New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$env:LOAF_SESSION_ID = [guid]::NewGuid().ToString('N')
Add-Content -LiteralPath $watchdogLog -Value "$(Get-Date -Format o) manual_session_start $env:LOAF_SESSION_ID"
Write-Host "LOAF live session: $env:LOAF_SESSION_ID"
Write-Host 'Bot onay istemeden canli maker islemlerine baslayacak. Durdurmak icin Ctrl+C.'

$finalExitCode = 0
while ($true) {
    & $python -m loaf_bot live
    $exitCode = $LASTEXITCODE
    Add-Content -LiteralPath $watchdogLog -Value "$(Get-Date -Format o) child_exit session=$env:LOAF_SESSION_ID code=$exitCode"

    if ($exitCode -eq 0) {
        Write-Host 'Bot normal olarak durdu.'
        break
    }
    if ($exitCode -eq 64 -or $exitCode -eq 65) {
        Write-Error "Kalici yapilandirma/preflight hatasi (exit $exitCode). Watchdog durdu." -ErrorAction Continue
        $finalExitCode = $exitCode
        break
    }
    if ($exitCode -eq 71) {
        Write-Warning 'LOAF islemleri gecici olarak durdurulmus. Watchdog 15 saniye sonra yeniden kontrol edecek.'
        Start-Sleep -Seconds 15
        continue
    }

    Write-Warning "Bot yeniden baslatma gerektiren bir durumla kapandi (exit $exitCode). Ayni oturumla 5 saniye sonra yeniden baslatiliyor."
    Start-Sleep -Seconds 5
}

exit $finalExitCode
