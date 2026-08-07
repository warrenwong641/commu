param(
    [Parameter(Mandatory = $true)]
    [string]$SshHost,
    [Parameter(Mandatory = $true)]
    [int]$SshPort,
    [string]$SshUser = "root",
    [string]$IdentityFile = "$HOME\.ssh\id_ed25519",
    [int]$LocalPort = 18443,
    [int]$RemotePort = 8443
)

$ErrorActionPreference = "Stop"
$identityPath = (Resolve-Path $IdentityFile).Path
$existingListener = Get-NetTCPConnection `
    -LocalPort $LocalPort `
    -State Listen `
    -ErrorAction SilentlyContinue
if ($existingListener) {
    throw "Local port $LocalPort is already in use."
}

$arguments = @(
    "-i", "`"$identityPath`"",
    "-p", $SshPort,
    "-N",
    "-T",
    "-L", "${LocalPort}:127.0.0.1:${RemotePort}",
    "-o", "BatchMode=yes",
    "-o", "Compression=no",
    "-o", "ExitOnForwardFailure=yes",
    "-o", "ServerAliveInterval=30",
    "-o", "StrictHostKeyChecking=accept-new",
    "${SshUser}@${SshHost}"
)
$process = Start-Process `
    -FilePath "ssh.exe" `
    -ArgumentList $arguments `
    -PassThru `
    -WindowStyle Hidden
Start-Sleep -Seconds 2
if ($process.HasExited) {
    throw "SSH tunnel exited with code $($process.ExitCode)."
}

[pscustomobject]@{
    ProcessId = $process.Id
    LocalEndpoint = "https://localhost:${LocalPort}/v1"
    RemoteTarget = "127.0.0.1:${RemotePort}"
}
