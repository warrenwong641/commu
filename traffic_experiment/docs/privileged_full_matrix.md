# Privileged one-GPU full matrix

The measured full matrix has a separate privileged release and supervisor. It
does not extend the pilots-only launcher and must not reuse the old two-worker
result root.

The fixed plan uses physical GPU 2 and one worker; the root-owned protocol
admission; baseline, rtt, and realistic networks; QA (32 samples) and summary
(20 units); TLS 1.3 and HTTP/3; three frozen conditions; and three repetitions.
This is exactly 2,808 calls.

## Trust and lifecycle

The credential-free bundle is independently authenticated by the root
installer. Archive links, duplicate/traversal paths, incomplete hashes, the
wrong model/GPU/manifests, and missing root-owned pilot admission are rejected.
The installed release is under /opt/commu-secure-matrix/releases/COMMIT/ and a
fresh result hierarchy is under /var/lib/commu-secure-matrix/COMMIT/.
The policy separately pins pilot release 7ed49eb0a04c3d4bd69e7361aab31de83426c61f;
the matrix commit is never substituted for that admission/service identity.

The supervisor exposes only check, status, run, and resume. It holds the global
topology lock and then the service .lock.d for the whole invocation. At every
cell boundary it checks service/config hashes, controller/API/engine identity
(PID, executable, argv, ticks), GPU/process inventory, listeners, admission,
network state, and Caddy.

Root owns the namespace, qdisc, Caddy, and exact cleanup lifecycle. The request
child enters the namespace, closes the lock descriptor, drops to the fixed
service UID/GID with cleared groups, and starts with an empty environment. That
request process reads the API key from the verified controller's /proc
environment; the key is not written, printed, exported, or passed in argv.

Failed attempts remain append-only. Resume skips only immutable completed
cells. Sealing moves a cell into a root-only directory, rejects symlinks and
hardlinks, verifies every result and capture, makes it root-owned/read-only,
and atomically returns it. The final marker binds all 12 cell inventories and
the immutable run plan. Unreadable inventory, identity drift, incomplete
sealing, or cleanup uncertainty fails closed.

## Build, install, and operate

From a clean committed checkout, copy privileged-matrix.env.example to an
ignored file and fill the exact model revision and manifest hashes. Build with:

    bash traffic_experiment/scripts/29_create_privileged_matrix_bundle.sh \
      --config ~/.config/commu/privileged-matrix.env \
      --qa-manifest traffic_experiment/artifacts/requests_32.jsonl \
      --summary-manifest traffic_experiment/artifacts/event_summaries_10.jsonl \
      --wheelhouse ~/.config/commu/qwen35-2b96b6b49f01/runner-wheelhouse-cp312-linux-x86_64-v1 \
      --service-state ~/.config/commu/qwen35-e12240f/service-attempt-5-gpu2-1.state \
      --service-user "$(id -un)" --service-uid "$(id -u)" --service-gid "$(id -g)" \
      --gpu-uuid GPU-41d1f86d-0197-51fe-c1ef-ad53c99e3223 \
      --output ~/.config/commu/commu-privileged-matrix.tar.gz

Copy the installer to a root-owned path and verify its separately published
SHA-256. Then install and use the exact printed supervisor path:

    sudo /root/commu-install-privileged-matrix-release.sh \
      ~/.config/commu/commu-privileged-matrix.tar.gz \
      BUNDLE_SHA256 REPOSITORY_SHA

    sudo /opt/commu-secure-matrix/releases/REPOSITORY_SHA/repository/\
    traffic_experiment/scripts/31_run_privileged_matrix.sh check

    sudo /opt/commu-secure-matrix/releases/REPOSITORY_SHA/repository/\
    traffic_experiment/scripts/31_run_privileged_matrix.sh run

Use status for validated progress and resume only with the same release,
admission, service/config identity, GPU, and run plan. Never point this
supervisor at the pilot output or old dual-worker tree.
