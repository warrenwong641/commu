# Lab-server migration and execution

This is the final controlled deployment. It removes AutoDL's SSH encapsulation
and gives the experiment direct control over TCP, UDP/QUIC, MTU, RTT, capacity,
and NIC offloads.

## What is transferred

Transfer the source tree plus the two frozen manifests:

- `artifacts/requests_32.jsonl`
- `artifacts/event_summaries_10.jsonl`

Do not transfer `server.env`, private keys, virtual environments, model caches,
or preliminary packet captures as executable inputs. Retain the AutoDL `runs/`
directory separately as an immutable pilot archive.

If Git is available, push/clone the exact experiment commit and copy the ignored
manifests separately. If it is not, create a credential-free archive:

```bash
bash traffic_experiment/scripts/16_create_lab_bundle.sh
sha256sum -c commu-lab-transfer.tar.gz.sha256
```

The bundler intentionally refuses to run until both frozen manifests have been
copied from AutoDL. This prevents accidentally changing sample selection or
compression when moving machines. Preserve
`commu-lab-transfer.tar.gz.manifests.sha256`; after extraction, verify it from
inside `traffic_experiment/`:

```bash
sha256sum -c ../commu-lab-transfer.tar.gz.manifests.sha256
```

On the lab server:

```bash
sudo mkdir -p /srv/commu
sudo tar -xzf commu-lab-transfer.tar.gz -C /srv/commu
cd /srv/commu/traffic_experiment
cp server.lab.env.example server.lab.env
```

Fill `LOCOMO_DATA_DIR`, `VLLM_BIN`, `VLLM_MODEL_REVISION`, and GPU IDs in
`server.lab.env`. Also copy the two exact digest values printed by the bundler
into `MANIFEST_SHA256` and `SUMMARY_MANIFEST_SHA256`; preflight rejects missing
or changed frozen artifacts. Keep credentials out of that file. Before a command
that needs the local key, load it only into the current shell:

```bash
read -rsp "Local vLLM API key: " LOCAL_VLLM_API_KEY
printf '\n'
export LOCAL_VLLM_API_KEY
```

For the root-owned matrix/session commands below, add
`LOCAL_VLLM_API_KEY` to `sudo --preserve-env` and unset it when the run ends.

## System preparation

The tested design assumes Ubuntu/Debian, two NVIDIA GPUs, root access, and enough
storage for the Qwen model, vLLM cache, manifests, and captures.

```bash
sudo apt-get update
sudo apt-get install -y \
  git curl ca-certificates \
  iproute2 ethtool iperf3 tshark

bash scripts/00_install_caddy.sh
bash scripts/01_setup_runner.sh
```

Install `uv` before the runner setup; this is the portable primary path because
`uv` obtains Python 3.11 and synchronizes the hashed runner and compression
locks into separate `.venv-runner` and `.venv-compression` environments. For a
non-uv fallback, install CPython 3.11 plus its `venv` support using packages
available for the server's exact distribution and release, then set
`PYTHON_BIN`. The bundled pip must support `--require-hashes`; setup does not
upgrade pip from the network. Do not assume every Ubuntu/Debian default
repository contains packages named `python3.11` and `python3.11-venv`.

Install vLLM in a separate CUDA environment following the version compatible
with the lab driver's CUDA runtime, then point `VLLM_BIN` at that environment.
Do not blindly copy the AutoDL vLLM environment: CUDA, PyTorch, flash-attention,
and driver combinations are machine-specific.

The locked compression environment is deliberately CPU-only and the shipped
profiles default `COMPRESSOR_DEVICE=cpu`. A GPU compressor must use a distinct
environment and pass a separately approved compatibility smoke test; the CPU
lock and the vLLM environment are not GPU-compressor validation evidence.

During `tshark` installation, either permit non-root capture and configure the
`dumpcap` group, or let the privileged matrix orchestrator invoke capture. The
matrix and session orchestrators require root because they create namespaces
and qdiscs. vLLM, Jupyter, manifest handling, and the read-only preflight should
run as the normal lab user.

## Model and environment validation

Start the two independent inference workers in a persistent terminal:

```bash
cd /srv/commu/traffic_experiment
EXPERIMENT_ENV_FILE="$PWD/server.lab.env" bash scripts/03_start_vllm_dual.sh
```

Use another terminal for the audit:

```bash
cd /srv/commu/traffic_experiment
EXPERIMENT_ENV_FILE="$PWD/server.lab.env" bash scripts/17_lab_preflight.sh
```

The audit records the OS, kernel, GPU UUIDs, driver, Caddy version, routes,
capture interfaces, network parameters, model ID, model revision, and the
4096-token limit under `runs/lab/machine_audit/`.

Before the final run, verify:

1. Both `/v1/models` endpoints respond on ports 8000 and 8001.
2. The model revision is a commit SHA rather than a moving branch.
3. `nvidia-smi` shows one vLLM worker per intended GPU.
4. Both manifests exist and their SHA-256 values are archived.
5. At least 100 GB remains for model files and packet captures.

## Controlled network topology

The client and secure proxy run in different Linux network namespaces joined by
a veth pair:

```text
llm-client (10.200.0.2)
          |
   MTU/rate/delay qdisc
          |
llmhost0 (10.200.0.1) -> Caddy TLS 1.3 or HTTP/3 -> vLLM
```

No SSH tunnel is involved. Caddy terminates direct TLS 1.3 on TCP and HTTP/3 on
UDP. The request runner and `dumpcap` execute inside `llm-client`, so packet
capture uses its visible `llmclient0` endpoint before Caddy's loopback vLLM
connection.

`scripts/11_network_condition.sh` provides three conditions:

- `baseline`: MTU 1500, offloads disabled, no artificial delay or capacity cap.
- `rtt`: MTU 1500 plus the configured round-trip delay.
- `realistic`: the same RTT plus asymmetric uplink/downlink limits.

The script disables TSO, GSO, GRO, and LRO on both veth endpoints. The physical
NIC is not part of this internal path, so its offload state does not contaminate
the captured experiment packets.

`scripts/15_calibrate_link.sh` runs uplink and downlink `iperf3` checks for every
condition. Calibration files are stored separately by condition.

## Output length and the 30-second rule

Both QA and event summarization use `max_tokens=4096`. This is an upper bound,
not a forced response length. `temperature=0` and the frozen prompt keep
generation reproducible, while `finish_reason` records whether the model stopped
naturally or reached the limit.

A 4096-token response may take several minutes. Per-request packet capture now
stops when the response completes; 900 seconds is only a safety ceiling. This
prevents the old 30-second capture from truncating a long response.

The 30-second rule is retained for the warm-session experiment: requests are
admitted sequentially until 30 seconds have elapsed, and the final admitted
response is allowed to finish. Analysis can later bin the complete capture into
one-second windows and report the first 30 seconds separately.

Every completed warm session also produces:

- `timeline_30s_segments.csv`: one indexed row per 30-second segment, including
  uplink/downlink packets, frame bytes, payload bytes, rates, prompts started,
  and responses active during that segment.
- `prompt_upload_events.csv`: exact application prompt-dispatch timestamp and
  segment index, logical JSON upload size, first/last observed upstream packet
  before the response, response timestamps, segments spanned, output tokens,
  and finish reason.

The application timestamp is the authoritative prompt-send marker. The
packet-derived upload interval is labelled as an on-wire estimate because TLS
and QUIC encryption prevents identifying individual prompt bytes directly;
for QUIC it can also contain transport acknowledgements.

## Resumable experiment commands

First run the one-request TLS and HTTP/3 pilots. Successful validation and
verified resource teardown create an immutable admission marker tied to the
manifest digests, model revision, Caddy version, MTU, and protocol-stack source:

```bash
sudo --preserve-env=PATH,LOCAL_VLLM_API_KEY \
  EXPERIMENT_ENV_FILE="$PWD/server.lab.env" \
  bash scripts/22_validate_protocol_pilots.sh
```

Each pilot first creates its own read-only evidence marker. Reuse is allowed
only when that marker still matches the seeded manifest-selected request,
request payload hash, model, generation settings, transport negotiation,
capture interface/filter, result-row hash, and nonempty PCAP hash. The PCAP is
revalidated before the shared admission marker is accepted.

Run the full per-request matrix:

```bash
sudo --preserve-env=PATH,LOCAL_VLLM_API_KEY \
  EXPERIMENT_ENV_FILE="$PWD/server.lab.env" \
  bash scripts/18_run_lab_matrix.sh
```

The matrix refuses to start without a matching successful protocol marker. It
then executes baseline, RTT-only, and realistic-capacity conditions across TLS
1.3, HTTP/3, QA, and summarization, with three repetitions. Completed
`(request_id, repetition)` jobs are skipped when the command is rerun.

Run the 30-second closed-loop sessions:

```bash
sudo --preserve-env=PATH,LOCAL_VLLM_API_KEY \
  EXPERIMENT_ENV_FILE="$PWD/server.lab.env" \
  SESSION_PROFILE=closed_loop_30s \
  bash scripts/19_run_lab_sessions.sh
```

The warm-session orchestrator enforces the same immutable protocol marker after
preflight and before creating its namespace or proxy. The marker digest includes
the warm-session runner and timeline implementation, so changing that measured
path requires new protocol pilots.

Run the separate Montieri-compatible timing profile:

```bash
sudo --preserve-env=PATH,LOCAL_VLLM_API_KEY \
  EXPERIMENT_ENV_FILE="$PWD/server.lab.env" \
  SESSION_PROFILE=antonio_10min \
  LAB_SESSION_NETWORKS="baseline" \
  bash scripts/19_run_lab_sessions.sh
```

The latter sends ten sequential prompts with a 60-second target start interval
over one warm connection. It must be reported separately from the 30-second
compression stress profile.

Each successful warm session receives a `SESSION_COMPLETE` marker. If a process
is interrupted, its partial PCAP and JSONL are preserved and the orchestrator
uses a `_retryN` session ID. It never merges separate TCP/QUIC connections into
one nominal warm session.

Never provide a sudo password to an AI agent or place it in an environment
file. A researcher or administrator should run the package-install command and
launch the privileged matrix/session command interactively from a trusted
console. The agent can perform all unprivileged setup, validate inputs, and
monitor the resulting logs.

## Operational lessons from AutoDL

- Download the model before the measured run; do not mix model transfer traffic
  with captures.
- Use persistent terminals (`tmux` or a system service) for vLLM and the runner.
- Preserve logs and results after every cell; the JSONL runner is append-only and
  resumable.
- Check the first completed TLS and QUIC captures before launching the full
  matrix. Confirm TCP versus UDP, TLS 1.3 versus QUIC, MTU, direction, and a
  non-truncated response.
- Do not combine AutoDL loopback/SSH results with the lab matrix. Keep them as
  pilot and route-calibration evidence.
