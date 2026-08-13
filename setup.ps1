[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Resolve-Python {
    $codexPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (Test-Path -LiteralPath $codexPython) {
        return $codexPython
    }

    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $pythonCommand) {
        return $pythonCommand.Source
    }

    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($null -ne $launcher) {
        return $launcher.Source
    }

    throw 'Python 3.11+ bulunamadi. Python kurun veya Codex Desktop runtime kurulumunu kontrol edin.'
}

$python = Resolve-Python
$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "Sanal ortam olusturuluyor: $python"
    if ([System.IO.Path]::GetFileName($python) -ieq 'py.exe') {
        & $python -3.11 -m venv .venv
    } else {
        & $python -m venv .venv
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Sanal ortam olusturulamadi (exit $LASTEXITCODE)."
    }
}

& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    throw "pip guncellenemedi (exit $LASTEXITCODE)."
}

& $venvPython -m pip install -e '.[dev]'
if ($LASTEXITCODE -ne 0) {
    throw "Bagimliliklar kurulamadi (exit $LASTEXITCODE)."
}

if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.env'))) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot '.env.example') -Destination (Join-Path $PSScriptRoot '.env')
    Write-Warning '.env olusturuldu. Yeni LOAF_API_KEY ve LOAF_USER_ID degerlerini yerel olarak doldurun.'
}

Write-Host 'Kurulum tamamlandi. .env doldurulduktan sonra .\run.ps1 calistirin.'
