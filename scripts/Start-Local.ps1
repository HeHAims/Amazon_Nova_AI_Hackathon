$ErrorActionPreference = "Stop"

try {
    $root = Split-Path -Parent $PSScriptRoot
    Set-Location $root

    $python = Join-Path $root ".venv\Scripts\python.exe"
    if (-not (Test-Path $python)) {
        throw "No se encontro .venv\\Scripts\\python.exe. Activa o crea el entorno virtual primero."
    }

    if (-not $env:AWS_REGION) { $env:AWS_REGION = "us-east-1" }
    if (-not $env:BEDROCK_MODEL_ID) { $env:BEDROCK_MODEL_ID = "amazon.nova-pro-v1:0" }
    if (-not $env:COSMOS_DB_NAME) { $env:COSMOS_DB_NAME = "cmis-database" }
    if (-not $env:COSMOS_CONTAINER_NAME) { $env:COSMOS_CONTAINER_NAME = "traces" }
    if (-not $env:APP_HOST) { $env:APP_HOST = "127.0.0.1" }
    if (-not $env:APP_PORT) { $env:APP_PORT = "8011" }

    if (-not $env:COSMOS_DB_CONNECTION_STRING) {
        throw "Falta COSMOS_DB_CONNECTION_STRING en variables de entorno."
    }

    Write-Host "Iniciando API en $($env:APP_HOST):$($env:APP_PORT) ..."
    & $python -m uvicorn main:app --host $env:APP_HOST --port $env:APP_PORT
}
catch {
    Write-Error $_
    exit 1
}
