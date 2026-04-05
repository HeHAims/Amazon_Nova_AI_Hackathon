param(
    [string]$CommitMessage = "feat: publish investor site updates",
    [string]$SwaAppName = "Symbiotic-site",
    [string]$ResourceGroup = "Symbiotic-site_group",
    [string]$SubscriptionId = "9a0404ef-5b68-4c75-8ae6-a10662b421be",
    [switch]$AllowDirty
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$staticRoot = Join-Path $repoRoot "static"
$sourceFiles = @("index.html", "styles.css", "benchmarks.html", "contact.html", "favicon.svg")

foreach ($fileName in $sourceFiles) {
    $sourcePath = Join-Path $repoRoot $fileName
    $targetPath = Join-Path $staticRoot $fileName

    if (-not (Test-Path $sourcePath)) {
        throw "Missing source file: $sourcePath"
    }

    Copy-Item $sourcePath $targetPath -Force
}

$gitDir = Join-Path $repoRoot ".git"
if (Test-Path $gitDir) {
    $dirty = git -C $repoRoot status --porcelain
    if ($dirty -and -not $AllowDirty) {
        throw "Repository has uncommitted changes. Commit/stash first or pass -AllowDirty."
    }

    git -C $repoRoot add @sourceFiles

    $staged = git -C $repoRoot diff --cached --name-only
    if ($staged) {
        git -C $repoRoot commit -m $CommitMessage
        git -C $repoRoot push origin main
    } else {
        Write-Host "No content changes detected for publish files. Skipping commit/push."
    }
} else {
    Write-Host "Git repository not found in $repoRoot. Skipping commit and push."
}

if (-not (Test-Path "$env:APPDATA\npm\swa.cmd")) {
    throw "SWA CLI not found at $env:APPDATA\npm\swa.cmd. Install it with npm i -g @azure/static-web-apps-cli."
}

& "$env:APPDATA\npm\swa.cmd" deploy $staticRoot --env production --app-name $SwaAppName --resource-group $ResourceGroup --subscription-id $SubscriptionId
