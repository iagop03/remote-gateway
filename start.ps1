<#
.SYNOPSIS
    Sets up (if needed) and starts Remote Gateway on localhost.
.EXAMPLE
    .\start.ps1
    .\start.ps1 -Port 9001
#>
param(
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 9000
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$venvPython = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    Write-Host "No .venv found, creating one..." -ForegroundColor Yellow
    python -m venv .venv
    & $venvPython -m pip install -e ".[test]" --quiet
}

if (-not (Test-Path ".env")) {
    Write-Host "No .env found, copying .env.example..." -ForegroundColor Yellow
    Copy-Item ".env.example" ".env"
}

if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Write-Host "Warning: 'claude' CLI not found on PATH. The claude-code driver will report unavailable." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Remote Gateway starting on http://${BindHost}:${Port}" -ForegroundColor Green
Write-Host "  Docs:    http://${BindHost}:${Port}/docs"
Write-Host "  Health:  http://${BindHost}:${Port}/health"
Write-Host "  Chat:    python scripts\chat.py --base-url http://${BindHost}:${Port}"
Write-Host ""

& $venvPython -m uvicorn remote_gateway.main:app --host $BindHost --port $Port
