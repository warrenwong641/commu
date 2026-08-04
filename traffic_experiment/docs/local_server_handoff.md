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

If the client and vLLM run on the same host through `127.0.0.1`, the capture
interface is normally `lo`. If they are placed in separate containers, select the
dedicated bridge/veth interface and change `VLLM_HOST` to the server-side address.

## One-time installation

Install Wireshark CLI tools using the server's package manager, then configure
dumpcap so your user can capture without running the entire experiment as root.
The exact administrator command is distribution-specific.

```bash
chmod +x scripts/*.sh
PYTHON_BIN=python3.11 ./scripts/01_setup_runner.sh
./.venv-runner/bin/python -m pip install -r requirements-compression.txt
```

vLLM should be installed in a CUDA-compatible environment appropriate for the
server. Set `VLLM_BIN` in `server.env` to its executable. Keeping vLLM installation
separate avoids changing a working CUDA/PyTorch environment.

Python 3.11 is recommended for the older, research-pinned LLMLingua dependency.
If it is unavailable, choose a compatible Python executable through `PYTHON_BIN`.

## Prepare prompts

Run this before starting vLLM:

```bash
./scripts/02_prepare_manifest.sh
```

This selects 32 eligible LoCoMo QA examples with seed 42 and writes 96 frozen
request rows: 32 uncompressed, 32 LongLLMLingua 2x, and 32 LongLLMLingua 4x.
Compression metadata and prompt hashes are saved in the manifest.

LongLLMLingua loads a separate compressor model. Let the command exit and confirm
its GPU memory has been released before starting Qwen.

## Start Qwen3.5-9B

In terminal 1:

```bash
./scripts/03_start_vllm.sh 2>&1 | tee vllm-server.log
```

Wait until the server reports that it is listening.

For the controlled two-GPU profile, start one server per GPU and port:

```bash
./scripts/03_start_vllm_dual.sh
```

The default mapping is GPU 0 to port 8000 and GPU 1 to port 8001.

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

For the two-GPU isolated pilot:

```bash
PROFILE=pilot ./scripts/run_local_experiment_parallel.sh
```

Each worker runs 36 serial trials. The workers execute concurrently with
port-specific captures, then their disjoint results are validated and merged.

## Run the main experiment

```bash
PROFILE=main ./scripts/run_local_experiment.sh
```

Use `PROFILE=main ./scripts/run_local_experiment_parallel.sh` for the two-GPU
isolated profile.

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
python -m traffic_experiment.traffic_measure.cli --help
python -m traffic_experiment.traffic_measure.cli prepare --help
python -m traffic_experiment.traffic_measure.cli run --help
python -m traffic_experiment.traffic_measure.cli analyze --help
```

The implementation uses the official LongLLMLingua `PromptCompressor`, vLLM's
OpenAI-compatible streaming Chat Completions endpoint, `dumpcap` for acquisition,
and `tshark` for offline metrics.
