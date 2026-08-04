# Hong Kong–China client path

## Current access method

AutoDL maps SSH through `connect.weste.seetacloud.com`. The local client uses
an SSH tunnel to reach the TLS proxy already listening on server port 8443:

```powershell
.\scripts\start_hk_ssh_tunnel.ps1 `
  -SshHost connect.weste.seetacloud.com `
  -SshPort 53363
```

The resulting endpoint is `https://localhost:18443/v1`. Copy the server's
Caddy root certificate to the client and pass it to the experiment runner:

```powershell
.\scripts\run_remote_client.ps1 `
  -BaseUrl https://localhost:18443/v1 `
  -TlsCaFile runs\hk_client\autodl-caddy-root.crt `
  -Transport tls13 `
  -Condition no_compression
```

The tunnel is suitable for functional testing and preliminary cross-border
capacity calibration. Its physical-interface packets are SSH packets, so it
must not be used as the final TLS/QUIC packet-size trace.

## Preliminary capacity observation

On 2026-08-04, with SSH compression disabled:

- server-to-Hong Kong random payload: 1.455 MiB/s (12.207 Mbit/s);
- Hong Kong-to-server random payload: 0.192 MiB/s in an end-to-end run that
  included SSH startup, followed by ready-state repetitions of 0.332 and
  0.553 MiB/s.

The path therefore did not show a fixed 100 KB/s cap, but the uplink was
variable and substantially slower than the downlink. These values are
calibration observations, not final experiment results.

## Direct-service requirement

For publishable packet-size measurements, expose a TLS/QUIC service directly
through AutoDL's custom-service mapping and capture the client physical
interface. AutoDL currently requires account verification before enabling
that mapping. Once enabled, repeat at least three capacity probes in each
direction before the LLM sessions.
