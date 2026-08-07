# Physical-Client-to-Server Protocol Validation

This is a separate validation gate from the namespace protocol gate
(`scripts/22_validate_protocol_pilots.sh`).  Data lives under
`runs/physical_validation/` and is never mixed with namespace data.

## Architecture

```text
Client terminal (second VS Code window, same remote desktop)
    │
    │ TLS 1.3 / TCP  or  HTTP/3 / UDP
    │
    v
Server (this machine, scripts/23_server_physical_listener.sh)
    ens20f0  144.214.210.31
    Caddy → vLLM 127.0.0.1:8000
    Capture on physical interface (ens20f0)
```

The client and server share a filesystem (same machine), so manifests,
CA cert, and output directories are directly accessible.  For a truly
remote client, substitute `scp` for file copies and adjust paths.

## Server Setup

A human starts the listener in the first terminal:

```bash
cd /home/wongshingyin/commu/traffic_experiment && \
sudo --preserve-env=PATH \
  EXPERIMENT_ENV_FILE="$PWD/server.lab.env" \
  LISTENER_PROFILE=high_ports \
  bash scripts/23_server_physical_listener.sh start
```

The listener script starts Caddy on `144.214.210.31`, records its PID and
state, and prints the CA certificate path. It does not modify firewall rules.
If a firewall change is required, apply the separately tracked rules:

```bash
sudo LISTENER_PROFILE=high_ports bash scripts/25_physical_firewall.sh apply
```

After validation, remove only this project's setup and verify closure:

```bash
sudo --preserve-env=PATH \
  EXPERIMENT_ENV_FILE="$PWD/server.lab.env" \
  LISTENER_PROFILE=high_ports \
  bash scripts/23_server_physical_listener.sh stop
sudo LISTENER_PROFILE=high_ports bash scripts/25_physical_firewall.sh cleanup
ss -ltnp | grep -E ':8443|:8543' || true
ss -lunp | grep -E ':8444|:8544' || true
```

## Client-Side Commands

Run from the second VS Code terminal (non-root).  The CA certificate
is on the same filesystem, so no copy needed.

### Prerequisites

```bash
cd /home/wongshingyin/commu/traffic_experiment
export PYTHONPATH=/home/wongshingyin/commu
RUNNER=.venv-runner/bin/python
CA_FILE=runs/lab/caddy/data/caddy/pki/authorities/local/root.crt
SERVER_IP=144.214.210.31
```

### 1. Verify endpoint health

```bash
$RUNNER -m traffic_experiment.traffic_measure.cli check \
  --base-url https://${SERVER_IP}:8443/v1 \
  --api-key "\${API_KEY}"
```

### 2. Calibrate physical path (outside capture)

```bash
mkdir -p runs/physical_validation/client
# Uplink: client → server
iperf3 -c ${SERVER_IP} -p 5201 -t 10 --json \
  > runs/physical_validation/client/uplink_calib.json
# Downlink: server → client
iperf3 -c ${SERVER_IP} -p 5202 -t 10 --json --reverse \
  > runs/physical_validation/client/downlink_calib.json
```

The server-side calibration (loopback) was already run by
`23_server_physical_validate.sh`.

### 3. TLS 1.3 single-request pilot

```bash
$RUNNER -m traffic_experiment.traffic_measure.cli run \
  --manifest artifacts/requests_32.jsonl \
  --output-dir runs/physical_validation/client/tls \
  --backend local_vllm \
  --base-url https://${SERVER_IP}:8443/v1 \
  --model Qwen/Qwen3-8B \
  --api-key "\${API_KEY}" \
  --samples 1 \
  --repetitions 1 \
  --seed 42 \
  --condition no_compression \
  --max-output-tokens 4096 \
  --request-timeout-seconds 900 \
  --observation-seconds 900 \
  --capture-stop-on-response \
  --capture-interface ens20f0 \
  --capture-filter "tcp port 8443" \
  --transport tls13 \
  --connection-mode warm \
  --tls-ca-file "${CA_FILE}"
```

### 4. Validate TLS PCAP (read-only)

```bash
PCAP=$(find runs/physical_validation/client/tls -name "*.pcapng" -type f | head -1)
T=.tools/usr/bin/tshark
echo "=== PCAP: ${PCAP} ==="
$T -r "${PCAP}" -Y "tcp" -T fields -e frame.number 2>/dev/null | wc -l
echo "TCP packets"
$T -r "${PCAP}" -Y "tls" -T fields -e frame.number 2>/dev/null | wc -l
echo "TLS records"
echo "max frame: $($T -r "${PCAP}" -T fields -e frame.len 2>/dev/null | sort -n | tail -1)"
# Direction
$T -r "${PCAP}" -Y "tcp.dstport==8443" -T fields -e frame.number 2>/dev/null | wc -l
echo "uplink"
$T -r "${PCAP}" -Y "tcp.srcport==8443" -T fields -e frame.number 2>/dev/null | wc -l
echo "downlink"
```

Checks: TCP packets > 0, TLS records > 0, max frame ≤ 1500, uplink > 0,
downlink > 0, no unrelated traffic on other ports.

### 5. HTTP/3 single-request pilot

```bash
$RUNNER -m traffic_experiment.traffic_measure.cli run \
  --manifest artifacts/requests_32.jsonl \
  --output-dir runs/physical_validation/client/http3 \
  --backend local_vllm \
  --base-url https://${SERVER_IP}:8444/v1 \
  --model Qwen/Qwen3-8B \
  --api-key "\${API_KEY}" \
  --samples 1 \
  --repetitions 1 \
  --seed 42 \
  --condition no_compression \
  --max-output-tokens 4096 \
  --request-timeout-seconds 900 \
  --observation-seconds 900 \
  --capture-stop-on-response \
  --capture-interface ens20f0 \
  --capture-filter "udp port 8444" \
  --transport http3 \
  --connection-mode warm \
  --tls-ca-file "${CA_FILE}"
```

### 6. Validate HTTP/3 PCAP

Same as step 4 but with `udp` and `quic` filters, port 8444, and
explicitly checking for **zero TCP packets** (no fallback).

```bash
PCAP=$(find runs/physical_validation/client/http3 -name "*.pcapng" -type f | head -1)
T=.tools/usr/bin/tshark
echo "UDP packets: $($T -r "${PCAP}" -Y "udp" -T fields -e frame.number 2>/dev/null | wc -l)"
echo "QUIC packets: $($T -r "${PCAP}" -Y "quic" -T fields -e frame.number 2>/dev/null | wc -l)"
echo "TCP packets (must be 0): $($T -r "${PCAP}" -Y "tcp" -T fields -e frame.number 2>/dev/null | wc -l)"
```

## Clock Sync

The client and server are the same machine — no clock sync needed.
For a truly remote client, synchronize with:

```bash
# Client side
sudo ntpdate -u 144.214.210.31
```

Record the round-trip offset before the experiment.

## Gating

Do not launch the full physical-client matrix until:
- TLS PCAP validates: TCP + TLS records, directions, MTU ≤ 1500, no unrelated traffic
- HTTP/3 PCAP validates: UDP + QUIC, zero TCP, directions, MTU ≤ 1500
- At least one successful iperf3 calibration in each direction

## Resumability

The runner is append-only and skips completed (request_id, repetition)
keys.  If a pilot is interrupted, rerun the same command — it resumes
where it left off.
