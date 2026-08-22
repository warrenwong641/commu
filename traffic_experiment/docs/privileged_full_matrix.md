# Privileged one-GPU full matrix

The measured 2,808-call matrix has a separate privileged release and
supervisor. It does not extend the pilots-only launcher and must never reuse a
result tree created by another commit, worker count, GPU index, or GPU UUID.

The fixed plan contains one verified vLLM worker; baseline, RTT, and realistic
networks; QA (32 samples) and summary (20 units); TLS 1.3 and HTTP/3; three
frozen prompt conditions; and three repetitions. The operator selects an
active service-state file at runtime. The release reads its physical GPU index
and UUID, verifies the live mapping and exclusive engine ownership, and scopes
all matrix output to that exact identity.

## Why protocol admission is required

The admission marker is proof that the selected service topology passed one
TLS 1.3 request and one HTTP/3 request using the frozen manifests and the same
model revision, GPU identity, MTU, Caddy version, and protocol-stack digest.
Without it, a matrix could record ordinary HTTPS, HTTP/3 fallback, traffic from
the wrong worker, or an incomplete capture and still look superficially
successful. The matrix supervisor therefore refuses to create `RUN_PLAN.json`
until the GPU-scoped, root-owned, mode-0444 admission marker is present and
cryptographically consistent.

Admission is produced by the separately installed portable pilot release:

1. start the one-GPU leased vLLM service;
2. run pilot `check --service-state STATE`;
3. run pilot `run --service-state STATE`;
4. run pilot `admission --service-state STATE`.

The same state file must then be supplied to the matrix supervisor.

## Immutable plan and output isolation

Each selected GPU receives a distinct root, and every independent run adds a
safe operator-chosen run ID:

    /var/lib/commu-secure-matrix/COMMIT/gpu-INDEX-GPU-UUID/runs/RUN_ID/

On a fresh `run`, the supervisor exclusively creates `RUN_PLAN.json` and
`worker-topology.json`. They bind:

- matrix and pilot repository SHA;
- installed release/config/admission digests;
- active-config digest and an append-only service-state generation ledger;
- both frozen manifest digests;
- physical GPU index and UUID, worker count, ports, model, served name, and
  model revision;
- the explicit run ID;
- the twelve network/workload/transport cells and exactly 2,808 calls.

Publication uses no-overwrite, no-follow semantics and read-only mode. A
`resume` recomputes the expected plan from the live service and release; any
mismatch refuses the resume. Lease PIDs and deadlines are intentionally not
part of the v2 measurement identity. Instead, before any request, every lease
activation publishes a root-owned state snapshot and immutable hash-chained
record under `SERVICE_GENERATIONS/`. Cleanup publishes a closure record. A new
generation is rejected while the prior generation is open, and final sealing
binds the closed ledger head. The final successful generation closes as
`ready-to-seal`; only `MATRIX_COMPLETE.json` means the entire experiment is
complete. This prevents accidental mixing of results from a
different GPU, topology, code revision, manifest, model, or admission while
allowing an expired lease to resume the same append-only experiment.

The older v1 plan format pinned its first service-state file directly. It is
never rewritten. A reviewed bridge release can continue such an incomplete
root only with an explicit source SHA:

```bash
sudo "$RUNNER" check --service-state "$STATE" --run-id "$RUN_ID" \
  --legacy-run-repository-sha c411237245e52e8efea5a12d5c651c5f26a68e39
sudo "$RUNNER" resume --service-state "$STATE" --run-id "$RUN_ID" \
  --legacy-run-repository-sha c411237245e52e8efea5a12d5c651c5f26a68e39
```

The bridge verifies both source release manifests, requires byte-identical
manifest projections of the measurement payloads (excluding generated Python
bytecode caches), executes the source release's request/network/protocol
tools and runtime, proves the old plan/config/admission/topology anchors, and
records the new bridge release plus the new lease snapshot. The old v1
`service_state_sha256` becomes the explicit generation-zero predecessor.
Without the option—or if any proof differs—`check` and `resume` fail closed.
After a bridged v1 run completes, use the bridge release for `status` and final
marker verification; the older v1 state tool does not understand the v2
completion marker that binds the generation ledger.

Failed attempts remain append-only. Resume skips only immutable completed
cells. Sealing rejects symlinks and hardlinks, verifies every result and
capture, moves the cell to root ownership/read-only mode, and publishes a cell
marker. The final completion marker binds all twelve sealed inventories and
the immutable run-plan digest.

## Trust and lifecycle

The credential-free bundle is authenticated by the root installer. Archive
links, traversal, incomplete hashes, wrong model/manifests, and unsafe paths
are rejected. Both root installers are excluded from the common reviewed-code
manifest and are authenticated independently by their published SHA-256
digests, avoiding a circular trust anchor.

The supervisor holds the global topology lock and the selected service
`.lock.d` for the entire invocation. At every cell boundary it rechecks the
service/config identities, GPU/process inventory, listener ownership,
admission, namespace/qdisc state, and Caddy. Root owns network setup and exact
cleanup; the request child enters the namespace and then drops to the fixed
service UID/GID with an empty environment. The API key is read only from the
verified controller process and is never written or placed in arguments.
Caddy disables its admin API and leaves the shared system trust store
unchanged; clients use the matrix-scoped CA snapshot explicitly. This prevents
future runs from adding trust entries but does not delete a certificate left by
an older run. Each network cell starts only after a bounded namespace check
completes both a TLS 1.3/HTTP/1.1 handshake and an HTTP/3/QUIC handshake against
the exact Caddy-owned listeners on the configured experiment-side veth address,
not a wildcard or public interface, without sending an HTTP request.
If Caddy exits during readiness, cleanup accepts that state only after proving
all protected proxy ports are closed; it then removes the exact recorded Caddy
state and continues namespace/veth teardown. A live PID with changed Caddy
identity or any remaining listener is ambiguous, so state is preserved and
cleanup fails closed.

## Build, install, and operate

From a clean committed checkout, fill the model revision and manifest hashes
in an ignored copy of `privileged-matrix.env.example`. Keep the GPU and scope
placeholders unchanged, then build:

```bash
bash traffic_experiment/scripts/29_create_privileged_matrix_bundle.sh \
  --config ~/.config/commu/privileged-matrix.env \
  --qa-manifest traffic_experiment/artifacts/requests_32.jsonl \
  --summary-manifest traffic_experiment/artifacts/event_summaries_10.jsonl \
  --wheelhouse ~/.config/commu/qwen35-2b96b6b49f01/runner-wheelhouse-cp312-linux-x86_64-v1 \
  --service-state-root ~/.config/commu \
  --service-user "$(id -un)" --service-uid "$(id -u)" --service-gid "$(id -g)" \
  --output ~/.config/commu/commu-privileged-matrix.tar.gz
```

Copy the installer to a root-owned path, verify its separately published
SHA-256, and install using the bundle digest and exact repository SHA. Installing
does not require or create an admission marker; runtime remains fail-closed
until the selected GPU has passed the pilots.

```bash
STATE=/home/wongshingyin/.config/commu/qwen35-COMMIT/service-gpuN-single-v1.state
RUNNER=/opt/commu-secure-matrix/releases/COMMIT/repository/traffic_experiment/scripts/31_run_privileged_matrix.sh
RUN_ID=main-YYYYMMDDThhmmssZ

sudo "$RUNNER" check --service-state "$STATE" --run-id "$RUN_ID"
sudo "$RUNNER" run --service-state "$STATE" --run-id "$RUN_ID"
sudo "$RUNNER" status --service-state "$STATE" --run-id "$RUN_ID"
sudo "$RUNNER" resume --service-state "$STATE" --run-id "$RUN_ID"
```

Use `run` only for a new GPU-scoped root. Use `resume` only for that exact
incomplete plan. A completed root is sealed and cannot be resumed. Never point
the supervisor at pilot output or an older dual-worker tree.

## Simpler bounded segments

The installed segment launcher replaces the GPU-specific setup, tmux, and
expiry scripts. The operator supplies a GPU only for a **new** independent run
and supplies a relative lease duration rather than calculating an absolute
epoch. The launcher derives the GPU UUID, per-attempt paths, hard deadline,
cleanup timer, service configuration, logs, and tmux name.

```bash
LAUNCHER=/opt/commu-secure-matrix/releases/COMMIT/repository/traffic_experiment/scripts/32_launch_privileged_matrix_segment.sh

# New independent run on GPU 4. This requires GPU-4 protocol admission.
sudo "$LAUNCHER" new \
  --run-id main-YYYYMMDDThhmmssZ \
  --gpu-index 4 \
  --lease 2h

# Continue an existing run. Its immutable RUN_PLAN.json selects the GPU.
sudo "$LAUNCHER" resume \
  --run-id main-YYYYMMDDThhmmssZ \
  --lease 115m
```

Accepted leases are 20 minutes through two hours (`20`, `115m`, or `2h`).
Cleanup begins ten minutes before the hard deadline. Starting another segment
does not repeat completed request keys: the matrix generation ledger closes the
old lease and `resume` appends a new generation. A GPU assertion may be supplied
on resume, but it must match the immutable plan. To use a different GPU, choose
a new run ID and keep its output root and analysis separate.

For a reviewed bridge continuing an older run, optionally narrow discovery to
the source commit:

```bash
sudo "$LAUNCHER" resume \
  --run-id main-YYYYMMDDThhmmssZ \
  --source-repository-sha SOURCE_COMMIT \
  --lease 2h
```

The launcher remains fail-closed when the selected GPU is occupied, its pilot
admission is absent, required ports are in use, the repository has drifted, or
the service configuration no longer matches the measured release.
