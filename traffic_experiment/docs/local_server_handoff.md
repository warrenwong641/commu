# Local Server Handoff

The code is ready to copy with the repository. Nothing in this workflow connects
to or modifies the lab server from the current machine.

## What you must fill in

After copying the repository to the server:

```bash
cd /ABSOLUTE/PATH/TO/commu/traffic_experiment
cp server.env.example server.env
nano server.env
```

The only mandatory blanks are:

- `LOCOMO_DATA_DIR`: absolute directory containing LoCoMo JSON files;
- `CAPTURE_INTERFACE`: interface listed by `dumpcap -D`.

Also review `CUDA_VISIBLE_DEVICES`, `TENSOR_PARALLEL_SIZE`, `MAX_MODEL_LEN`,
`GPU_MEMORY_UTILIZATION`, `VLLM_BIN`, and `COMPRESSOR_MODEL`.

Do not add credentials to `server.env`. Before starting vLLM or a local
measurement, read the key into that terminal's process environment:

```bash
read -rsp "Local vLLM API key: " LOCAL_VLLM_API_KEY
printf '\n'
export LOCAL_VLLM_API_KEY
```

Unset it when the server and measurement processes have stopped.

If the client and vLLM run on the same host through `127.0.0.1`, the capture
interface is normally `lo`. If they are placed in separate containers, select the
dedicated bridge/veth interface and change `VLLM_HOST` to the server-side address.

## One-time installation

Install Wireshark CLI tools using the server's package manager, then configure
dumpcap so your user can capture without running the entire experiment as root.
The exact administrator command is distribution-specific.

```bash
chmod +x scripts/*.sh
./scripts/01_setup_runner.sh
```

The setup command creates `.venv-runner` from the hashed runner lock and a
distinct `.venv-compression` from the hashed compression lock. It supports both
uv and an existing CPython 3.11 plus its bundled pip; both paths require the same
SHA-256 hashes. Do not install `requirements-compression.txt` into
`.venv-runner`: that file is a lock input, not an installation artifact.
The reproducible compression environment is CPU-only and defaults
`COMPRESSOR_DEVICE=cpu`; its lock does not install CUDA packages.

vLLM should be installed in a CUDA-compatible environment appropriate for the
server. Set `VLLM_BIN` in `server.env` to its executable. Keeping vLLM installation
separate avoids changing a working CUDA/PyTorch environment.

Python 3.11 is required for the reproducible runner and the older,
research-pinned LLMLingua dependency. The portable primary path is `uv`, which
obtains Python 3.11 itself. The non-uv fallback requires an existing CPython
3.11 interpreter with `venv` and a bundled pip that supports
`--require-hashes`; it performs no pip upgrade. Whether a distribution supplies
that interpreter in its default package repositories depends on the
distribution and release. Select a suitable executable through `PYTHON_BIN`;
the setup script refuses other Python feature versions.

## Prepare prompts

Run this before starting vLLM:

```bash
./scripts/02_prepare_manifest.sh
```

Because the default conditions include LongLLMLingua, this command selects
`.venv-compression/bin/python`. A no-compression-only summary preparation uses
the runner; any summary compression condition selects the compression
interpreter. Both paths run from a safe temporary working directory with Python
safe-path mode and a repository-only import path.

This selects 32 eligible LoCoMo QA examples with seed 42 and writes 96 frozen
request rows: 32 uncompressed, 32 LongLLMLingua 2x, and 32 LongLLMLingua 4x.
Compression metadata and prompt hashes are saved in the manifest.

LongLLMLingua loads a separate compressor model. Let the preparation command
exit before starting Qwen so model preparation and traffic measurement remain
isolated. GPU compression is not validated by the CPU lock. It requires a
separate environment and a separately approved compatibility smoke test whose
recorded `GPU_COMPRESSOR_APPROVED_PYTHON` resolves to the selected
`COMPRESSION_PYTHON`; do not point the CPU-only `.venv-compression` at CUDA.
Use `scripts/02_prepare_manifest.sh` for CPU preparation. The parallel manifest
entry point rejects CPU mode before starting a compressor so two 7B processes
cannot silently consume memory at the same time.

## Start Qwen3.5-9B

In terminal 1:

```bash
./scripts/03_start_vllm.sh 2>&1 | tee vllm-server.log
```

Wait until the server reports that it is listening.

For the controlled worker profile, set an exact mapping in the untracked env:

```bash
# One worker, physical GPU 2
PARALLEL_WORKERS=1
CUDA_VISIBLE_DEVICES=2

# Or two workers, physical GPUs 2 then 1
PARALLEL_WORKERS=2
CUDA_VISIBLE_DEVICES=2,1
```

Then start one server per selected GPU and port:

```bash
./scripts/03_start_vllm_dual.sh
```

Worker 0 uses port 8000; optional worker 1 uses port 8001. Always use a fresh
`RUNS_ROOT` when changing worker count or GPU mapping.

## Run a pilot

In terminal 2:

```bash
PROFILE=pilot ./scripts/run_local_experiment.sh
```

This performs 8 samples x 3 conditions x 3 repetitions = 72 measured requests.
It checks the endpoint and capture interface first, then writes:

- `runs/local_vllm_pilot/results.jsonl`;
- one `.pcapng` file per request;
- `runs/local_vllm_pilot/traffic_metrics.csv`.

Inspect these outputs before starting the full experiment.

For the one- or two-GPU isolated pilot:

```bash
PROFILE=pilot ./scripts/run_local_experiment_parallel.sh
```

Trials are deterministically sharded across the configured worker count. With
two workers they execute concurrently; with one, only the primary ports are
used. Their disjoint results are validated and merged.

## Run the main experiment

```bash
PROFILE=main ./scripts/run_local_experiment.sh
```

Use `PROFILE=main ./scripts/run_local_experiment_parallel.sh` for the switchable
one- or two-worker isolated profile.

By default this performs 32 samples x 3 conditions x 3 technical repetitions =
288 measured requests. Set `MAIN_REPETITIONS` only when the pilot variance or a
power analysis justifies a different count.
The runner is resumable: restarting the same profile skips successful
request/repetition pairs already present in `results.jsonl`. Failed trials remain
in the log and are attempted again.

For the optional ten-repetition profile:

```bash
PROFILE=robustness ./scripts/run_local_experiment.sh
```

## Safety checks before the main run

- Verify one capture manually in Wireshark.
- Confirm that only the intended vLLM port appears.
- Confirm all successful rows have request and capture SHA-256 hashes.
- Confirm output text and token usage are present.
- Check `capture_may_be_truncated`; it should be false.
- Compare the manifest's actual compression ratios with the 2x/4x targets.
- Keep `server.env`, captures, and run outputs out of Git.

## Direct commands without wrappers

All shell scripts call the same Python CLI:

```bash
REPOSITORY_ROOT="$(cd .. && pwd)"
(
  cd "${TMPDIR:-/tmp}"
  env -u PYTHONHOME PYTHONPATH="${REPOSITORY_ROOT}" PYTHONSAFEPATH=1 \
    "${REPOSITORY_ROOT}/traffic_experiment/.venv-compression/bin/python" -P \
    -m traffic_experiment.traffic_measure.cli prepare --help
  env -u PYTHONHOME PYTHONPATH="${REPOSITORY_ROOT}" PYTHONSAFEPATH=1 \
    "${REPOSITORY_ROOT}/traffic_experiment/.venv-runner/bin/python" -P \
    -m traffic_experiment.traffic_measure.cli run --help
  env -u PYTHONHOME PYTHONPATH="${REPOSITORY_ROOT}" PYTHONSAFEPATH=1 \
    "${REPOSITORY_ROOT}/traffic_experiment/.venv-runner/bin/python" -P \
    -m traffic_experiment.traffic_measure.cli analyze --help
)
```

The implementation uses the official LongLLMLingua `PromptCompressor`, vLLM's
OpenAI-compatible streaming Chat Completions endpoint, `dumpcap` for acquisition,
and `tshark` for offline metrics.
