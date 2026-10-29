[CmdletBinding()]
param(
    [ValidateSet("web", "generate")]
    [string]$Mode = "web",
    [string]$Workspace,
    [string]$Output,
    [string]$RunId,
    [string]$EntityName,
    [string]$EntityId,
    [string]$EntityType,
    [ValidateRange(1, 20)]
    [int]$QuestionCount = 1,
    [string[]]$Providers = @("hgnc", "ncbi_gene", "ensembl", "primekg", "pubmed"),
    [string]$CtdIndex,
    [string]$HostAddress = "127.0.0.1",
    [ValidateRange(1, 65535)]
    [int]$Port = 8765,
    [string]$Python = "D:\Anaconda\envs\qa_env\python.exe",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$pythonPath = Join-Path $projectRoot "src"
$Providers = @(
    $Providers |
        ForEach-Object { "$_" -split "," } |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ }
)

function Resolve-ProjectPath {
    param([string]$Path, [string]$DefaultRelative)
    $candidate = if ($Path) { $Path } else { $DefaultRelative }
    if ([IO.Path]::IsPathRooted($candidate)) {
        return [IO.Path]::GetFullPath($candidate)
    }
    return [IO.Path]::GetFullPath((Join-Path $projectRoot $candidate))
}

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python executable not found: $Python"
}
$Python = (Resolve-Path -LiteralPath $Python).Path
$env:PYTHONPATH = $pythonPath

if ($Mode -eq "generate" -and -not $EntityName) {
    throw "EntityName is required when Mode=generate"
}
if (-not $RunId) {
    $RunId = "objective-" + (Get-Date -Format "yyyyMMdd-HHmmss")
}
if ($RunId -notmatch "^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$") {
    throw "RunId contains unsupported characters: $RunId"
}
if ($Providers.Count -eq 0) {
    throw "At least one provider is required"
}
if (($Providers | Where-Object { $_ -notin @("primekg", "ctd", "pubmed", "hgnc", "ncbi_gene", "ensembl") }).Count -gt 0) {
    throw "Providers must be selected from primekg, ctd, pubmed, hgnc, ncbi_gene and ensembl"
}
if ($Providers -contains "ctd" -and -not $CtdIndex) {
    $evidenceConfig = Join-Path $projectRoot "config\evidence_enrichment.json"
    if (Test-Path -LiteralPath $evidenceConfig -PathType Leaf) {
        $CtdIndex = (Get-Content -LiteralPath $evidenceConfig -Raw | ConvertFrom-Json).ctd_index_path
    }
    if (-not $CtdIndex) {
        throw "CtdIndex is required when the ctd provider is enabled"
    }
}

if ($Mode -eq "web") {
    $Workspace = Resolve-ProjectPath $Workspace "data\runs\objective-web"
    $arguments = @(
        "-m", "medical_qa_data_factory", "web", "serve",
        "--workspace", $Workspace,
        "--host", $HostAddress,
        "--port", "$Port"
    )
}
else {
    $Workspace = Resolve-ProjectPath $Workspace "data\runs\$RunId"
    $Output = Resolve-ProjectPath $Output "data\releases\$RunId"
    $arguments = @(
        "-m", "medical_qa_data_factory", "objective", "generate",
        "--workspace", $Workspace,
        "--output", $Output,
        "--run-id", $RunId,
        "--entity-name", $EntityName,
        "--question-count", "$QuestionCount"
    )
    if ($EntityId) { $arguments += @("--entity-id", $EntityId) }
    if ($EntityType) { $arguments += @("--entity-type", $EntityType) }
    foreach ($provider in $Providers) {
        $arguments += @("--provider", $provider)
    }
    if ($CtdIndex) {
        $arguments += @("--ctd-index", (Resolve-ProjectPath $CtdIndex $CtdIndex))
    }
}

$modelReady = $false
$modelDetail = "model status unavailable"
Push-Location $projectRoot
try {
    $modelOutput = & $Python -m medical_qa_data_factory model providers 2>$null
    if ($LASTEXITCODE -eq 0 -and $modelOutput) {
        $modelStatus = $modelOutput | ConvertFrom-Json
        $readyProvider = $modelStatus.providers |
            Where-Object { $_.api_key_present } |
            Select-Object -First 1
        if ($readyProvider) {
            $modelReady = $true
            $modelDetail = "$($readyProvider.provider_id) / $($readyProvider.model_id)"
        }
        else {
            $modelDetail = "model API key is not configured"
        }
    }
}
catch {
    $modelDetail = $_.Exception.Message
}
finally {
    Pop-Location
}

if ($DryRun) {
    [pscustomobject]@{
        mode = $Mode
        project_root = $projectRoot
        python = $Python
        workspace = $Workspace
        output = $Output
        run_id = $RunId
        model_ready = $modelReady
        model_detail = $modelDetail
        arguments = $arguments
    } | ConvertTo-Json -Depth 5
    exit 0
}

if (-not $modelReady) {
    if ($Mode -eq "generate") {
        throw "The objective main agent requires a configured model: $modelDetail"
    }
    Write-Warning "The main-agent engine will be disabled until a model API key is configured: $modelDetail"
}

if ($Mode -eq "web") {
    Write-Host "Objective workbench: http://$HostAddress`:$Port"
    Write-Host "Workspace: $Workspace"
}
else {
    Write-Host "Starting objective run: $RunId"
    Write-Host "Workspace: $Workspace"
    Write-Host "Release:   $Output"
}

Push-Location $projectRoot
try {
    & $Python @arguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
