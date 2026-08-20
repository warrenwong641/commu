# Privileged protocol-pilot release

The namespace pilots need root for `ip`, `tc`, Caddy, and packet capture. Do
not run `scripts/22_validate_protocol_pilots.sh` from a user-writable checkout
or preserve a user's `PATH` into `sudo`. The supported privileged path is a
hash-verified release containing the exact committed source, non-secret frozen
configuration, manifests, pinned Caddy, and a wheel-only runner environment.

This launcher can run only two requests: one TLS 1.3 QA pilot and one HTTP/3 QA
pilot. It has no matrix action and does not invoke the matrix or session
orchestrators.

## Trust boundary

The bundle is created without privilege. The root bootstrap then:

1. opens the bundle once and copies from that pinned file descriptor;
2. checks the archive SHA-256 for copy integrity, then independently authorizes
   executable bytes through the digest embedded in the reviewed installer;
3. pins the exact account, service-state root, model/revision, served name, and
   two frozen manifest digests; each invocation then selects one state file and
   verifies its GPU index/UUID against the live NVIDIA inventory;
4. rejects absolute paths, traversal, duplicate entries, links, devices, and
   oversized expansion;
5. rebuilds CPython 3.12 with `--copies`, offline, from the exact committed
   wheel digest manifest and minimal hash-locked pilot requirements;
6. verifies a complete per-file digest manifest;
7. makes the release root-owned and non-writable; and
8. smoke-tests the now-root-owned Python and Caddy runtimes.

The installed runner starts with an empty environment and a fixed system path.
It never sources or executes the user's checkout, active service config, or
venv. It parses the user-owned vLLM state/config as inert data, verifies the
recorded controller, API process, engine process, listener, GPU index/UUID, and
config digest, and then reads the API key only from the verified controller's
`/proc` environment. The key remains in process memory: it is not printed,
written, or passed in an argument.

The installer publishes a root-owned lock inode at
`/run/lock/commu-protocol-pilots/vllm-topology-UID.lock`, writable by the service user's primary
group but not removable by that user. Both the topology switcher and privileged
pilot runner take its exclusive `flock`. The runner also takes the active
state's `.lock.d` compatibility lock. Both remain held from the identity check
through immutable protocol-admission publication, so a topology switch cannot
start during the pilots. The pilot subprocess creates only a read-only
admission candidate; its privileged parent rechecks the service and teardown,
then publishes `PROTOCOL_VALIDATION_OK`. `ss` and `ip` inventory errors are
fatal rather than being interpreted as an empty system.

Pilot output is isolated by the verified GPU identity under
`/var/lib/commu-protocol-pilots/COMMIT/gpu-INDEX-GPU-UUID/`, whose complete
ancestor chain is root-owned. The installed source/runtime is under
`/opt/commu-protocol-pilots/releases/COMMIT/`.

## Administrator prerequisites

Install these through the server's package/service-management policy:

- CPython 3.12 plus `python3.12-venv`, Git, curl, `iproute2`, ethtool, iperf3,
  tshark/dumpcap, and the
  NVIDIA user-space tools matching the installed driver;
- a Caddy binary with HTTP/3 support. By default the builder copies
  `traffic_experiment/.tools/caddy` into the release; the source binary is
  never executed by root. It is covered by the per-file digest manifest and is
  made root-owned before its first privileged smoke test or pilot execution.

The audited server currently needs these administrator repairs before the
installer can run. The installer deliberately fails before release deployment
while either prerequisite is absent:

```bash
sudo chown root:root / /home
sudo chmod 0755 / /home
sudo apt-get update
sudo apt-get install --no-install-recommends python3.12-venv
```

Recheck with `stat -c '%U:%G %a %n' / /home` and
`/usr/bin/python3 -m venv --copies /tmp/commu-venv-probe`; remove only that
new probe directory after the command succeeds. Do not weaken the installer
path checks to work around the anomalous ownership.

The installer never uses the user venv or network. It creates a root-owned
CPython 3.12 venv offline and installs only 16 pinned wheels. The minimal pilot
runtime intentionally omits ROUGE/numpy/NLTK: protocol pilots produce transport
evidence, not post-hoc answer-quality analysis.

## Build without sudo

Start from a clean committed checkout whose commit is also recorded in the
running single-worker service state. Copy the example to an ignored file, fill
the exact model revision and both manifest hashes, and keep `VLLM_BIN` set to
`/usr/bin/false` because this release never launches vLLM:

```bash
cp traffic_experiment/privileged-pilot.env.example \
  ~/.config/commu/privileged-pilot.env
chmod 0600 ~/.config/commu/privileged-pilot.env

mkdir -p ~/.config/commu/qwen35-2b96b6b49f01/runner-wheelhouse-cp312-linux-x86_64-v1
# Populate this directory as an unprivileged user from the 16 filenames in
# privileged-pilot-wheels.cp312-linux-x86_64.sha256. The builder verifies exact
# filenames and hashes before it creates a bundle.

bash traffic_experiment/scripts/26_create_privileged_pilot_bundle.sh \
  --config ~/.config/commu/privileged-pilot.env \
  --qa-manifest traffic_experiment/artifacts/requests_32.jsonl \
  --summary-manifest traffic_experiment/artifacts/event_summaries_10.jsonl \
  --wheelhouse ~/.config/commu/qwen35-2b96b6b49f01/runner-wheelhouse-cp312-linux-x86_64-v1 \
  --service-state-root ~/.config/commu \
  --service-user "$(id -un)" --service-uid "$(id -u)" --service-gid "$(id -g)" \
  --output ~/.config/commu/commu-privileged-pilot.tar.gz
```

The builder refuses tracked or staged changes, validates the manifest hashes,
forces Git archive and checksum text modes so the reviewed-code manifest does
not depend on a builder's `core.autocrlf`/`core.eol` settings, verifies exact
CPython-3.12/Linux wheel bytes, rejects links, and prints the repository and
bundle SHA values. It never reads the vLLM API key.

## Install and run with sudo

First copy the bootstrap itself into a root-owned path, then check it against
the SHA-256 published from the reviewed commit. Do not execute it from the
checkout.

```bash
sudo install -o root -g root -m 0700 \
  traffic_experiment/scripts/27_install_privileged_pilot_release.sh \
  /root/commu-install-privileged-pilot-release.sh

printf '%s  %s\n' INSTALLER_SHA256 \
  /root/commu-install-privileged-pilot-release.sh |
  sudo sha256sum --check --strict -

sudo /root/commu-install-privileged-pilot-release.sh \
  ~/.config/commu/commu-privileged-pilot.tar.gz \
  BUNDLE_SHA256 REPOSITORY_SHA
```

Use the exact installed path printed by the installer. Set `SERVICE_STATE` to
the running one-worker state for the GPU being tested; the same selected state
must be supplied to `check`, `run`, and `admission`:

```bash
SERVICE_STATE=/home/wongshingyin/.config/commu/qwen35-e12240f/service-gpu4.state

sudo /opt/commu-protocol-pilots/releases/REPOSITORY_SHA/repository/\
traffic_experiment/scripts/28_run_privileged_protocol_pilots.sh \
  check --service-state "${SERVICE_STATE}"

sudo /opt/commu-protocol-pilots/releases/REPOSITORY_SHA/repository/\
traffic_experiment/scripts/28_run_privileged_protocol_pilots.sh \
  run --service-state "${SERVICE_STATE}"

sudo /opt/commu-protocol-pilots/releases/REPOSITORY_SHA/repository/\
traffic_experiment/scripts/28_run_privileged_protocol_pilots.sh \
  admission --service-state "${SERVICE_STATE}"
```

`check` performs no generation. `run` performs only the two one-request pilots
and publishes admission after complete teardown and a post-pilot service
identity check. `admission` only revalidates existing immutable evidence.
The release is not pinned to a numbered GPU: it accepts any installed GPU whose
selected state, active config, live index-to-UUID mapping, process ownership,
and exclusive engine all pass the reviewed checks. It does not select an idle
GPU or move vLLM; the one-worker service must already be running on that GPU.

This release does not complete the one-GPU experiment. A one-GPU full matrix
still needs a separate reviewed root-owned launcher/release and explicit run
authorization. In particular, never resume the old two-worker matrix tree into
this topology: its worker/GPU blocking factor differs, and unreadable or absent
completion state is not evidence that it completed.

If a pilot fails, the existing owned-resource lifecycle keeps its precise state
when cleanup cannot be verified. Inspect that state before any manual action;
never remove a namespace, listener, or qdisc merely because its name matches.
