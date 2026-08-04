param(
    [string]$BaseUrl = "http://127.0.0.1:18000/v1",
    [string]$Manifest = "artifacts/requests_32.jsonl",
    [string]$OutputDir = "runs/remote_hk_client",
    [string]$Model = "Qwen/Qwen3.5-9B",
    [int]$Samples = 8,
    [int]$Repetitions = 3,
    [int]$MaxOutputTokens = 1024,
    [string]$ApiKey = "local-test-key"
)

$ErrorActionPreference = "Stop"
$experimentRoot = Split-Path -Parent $PSScriptRoot
$repositoryRoot = Split-Path -Parent $experimentRoot
$manifestPath = Join-Path $experimentRoot $Manifest
$outputPath = Join-Path $experimentRoot $OutputDir

Push-Location $repositoryRoot
try {
    python -m traffic_experiment.traffic_measure.cli check `
        --base-url $BaseUrl `
        --api-key $ApiKey

    python -m traffic_experiment.traffic_measure.cli run `
        --manifest $manifestPath `
        --output-dir $outputPath `
        --backend local_vllm `
        --base-url $BaseUrl `
        --model $Model `
        --samples $Samples `
        --repetitions $Repetitions `
        --seed 42 `
        --temperature 0 `
        --max-output-tokens $MaxOutputTokens `
        --request-timeout-seconds 180 `
        --observation-seconds 0 `
        --transport http1 `
        --connection-mode warm `
        --no-capture
}
finally {
    Pop-Location
}
