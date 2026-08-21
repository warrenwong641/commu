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
- service-state and active-config digests;
- both frozen manifest digests;
- physical GPU index and UUID, worker count, ports, model, served name, and
  model revision;
- the explicit run ID;
- the twelve network/workload/transport cells and exactly 2,808 calls.

Publication uses no-overwrite, no-follow semantics and read-only mode. A
`resume` recomputes the expected plan from the live service and release; any
mismatch refuses the resume. This prevents accidental mixing of results from a
different GPU, topology, code revision, manifest, model, or admission.

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
