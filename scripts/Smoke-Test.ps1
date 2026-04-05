$ErrorActionPreference = "Stop"

try {
    if (-not $env:APP_HOST) { $env:APP_HOST = "127.0.0.1" }
    if (-not $env:APP_PORT) { $env:APP_PORT = "8011" }

    $baseUrl = "http://$($env:APP_HOST):$($env:APP_PORT)"

    $health = Invoke-WebRequest -UseBasicParsing "$baseUrl/"
    if ($health.StatusCode -ne 200) {
        throw "Health check fallo con status $($health.StatusCode)."
    }

    $okPrompt = @{ prompt = "Dame 3 acciones practicas para mejorar la comunicacion del equipo." } | ConvertTo-Json
    $okResponse = Invoke-RestMethod -Method Post -Uri "$baseUrl/generate" -ContentType "application/json" -Body $okPrompt

    $longPromptText = "x" * 5000
    $longPrompt = @{ prompt = $longPromptText } | ConvertTo-Json

    $longStatus = $null
    try {
        Invoke-RestMethod -Method Post -Uri "$baseUrl/generate" -ContentType "application/json" -Body $longPrompt | Out-Null
    }
    catch {
        $longStatus = $_.Exception.Response.StatusCode.value__
    }

    if ($longStatus -ne 413) {
        throw "La validacion de prompt largo no devolvio 413. Status recibido: $longStatus"
    }

    Write-Host "Smoke test OK"
    Write-Host "- GET / => 200"
    Write-Host "- POST /generate => $($okResponse.status)"
    Write-Host "- Prompt largo => 413"
}
catch {
    Write-Error $_
    exit 1
}
