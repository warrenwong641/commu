param(
    [string]$BaseUrl = "http://127.0.0.1:18000/v1",
    [string]$Manifest = "artifacts/requests_32.jsonl",
    [string]$OutputDir = "runs/remote_hk_client",
    [string]$Model = "Qwen/Qwen3.5-9B",
    [int]$Samples = 2,
    [int]$Repetitions = 3,
    [int]$MaxOutputTokens = 1024,
    [double]$SessionBudgetSeconds = 30,
    [ValidateSet("no_compression", "longllmlingua_2x", "longllmlingua_4x")]
    [string]$Condition = "no_compression",
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

    for ($repetition = 1; $repetition -le $Repetitions; $repetition++) {
        $sessionId = "hk-" + (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssfffZ")
        $sessionOutput = Join-Path $outputPath ("session-{0:D2}" -f $repetition)
        python -m traffic_experiment.traffic_measure.cli run `
            --manifest $manifestPath `
            --output-dir $sessionOutput `
            --backend local_vllm `
            --base-url $BaseUrl `
            --model $Model `
            --samples $Samples `
            --repetitions 1 `
            --seed 42 `
            --temperature 0 `
            --max-output-tokens $MaxOutputTokens `
            --request-timeout-seconds 180 `
            --observation-seconds 0 `
            --transport http1 `
            --connection-mode warm `
            --session-id $sessionId `
            --condition $Condition `
            --session-budget-seconds $SessionBudgetSeconds `
            --no-capture
    }
}
finally {
    Pop-Location
}
