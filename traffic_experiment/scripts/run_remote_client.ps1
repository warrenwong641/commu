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
    [ValidateSet("http1", "tls13")]
    [string]$Transport = "http1",
    [string]$TlsCaFile = ""
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($env:LOCAL_VLLM_API_KEY)) {
    throw "Set LOCAL_VLLM_API_KEY in the current process environment."
}
$experimentRoot = Split-Path -Parent $PSScriptRoot
$repositoryRoot = Split-Path -Parent $experimentRoot
$manifestPath = Join-Path $experimentRoot $Manifest
$outputPath = Join-Path $experimentRoot $OutputDir
$resolvedTlsCaFile = $null
if ($TlsCaFile) {
    $resolvedTlsCaFile = (Resolve-Path $TlsCaFile).Path
}
$previousSslCertFile = $env:SSL_CERT_FILE

Push-Location $repositoryRoot
try {
    if ($resolvedTlsCaFile) {
        $env:SSL_CERT_FILE = $resolvedTlsCaFile
    }
    python -m traffic_experiment.traffic_measure.cli check `
        --base-url $BaseUrl
    if ($LASTEXITCODE -ne 0) {
        throw "Endpoint check failed with exit code $LASTEXITCODE."
    }

    for ($repetition = 1; $repetition -le $Repetitions; $repetition++) {
        $sessionId = "hk-" + (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssfffZ")
        $sessionOutput = Join-Path $outputPath ("session-{0:D2}" -f $repetition)
        $runArguments = @(
            "-m", "traffic_experiment.traffic_measure.cli", "run",
            "--manifest", $manifestPath,
            "--output-dir", $sessionOutput,
            "--backend", "local_vllm",
            "--base-url", $BaseUrl,
            "--model", $Model,
            "--samples", $Samples,
            "--repetitions", 1,
            "--seed", 42,
            "--temperature", 0,
            "--max-output-tokens", $MaxOutputTokens,
            "--request-timeout-seconds", 180,
            "--observation-seconds", 0,
            "--transport", $Transport,
            "--connection-mode", "warm",
            "--session-id", $sessionId,
            "--condition", $Condition,
            "--session-budget-seconds", $SessionBudgetSeconds,
            "--no-capture"
        )
        if ($resolvedTlsCaFile) {
            $runArguments += @("--tls-ca-file", $resolvedTlsCaFile)
        }
        python @runArguments
        if ($LASTEXITCODE -ne 0) {
            throw "Experiment run failed with exit code $LASTEXITCODE."
        }
    }
}
finally {
    Pop-Location
    $env:SSL_CERT_FILE = $previousSslCertFile
}
