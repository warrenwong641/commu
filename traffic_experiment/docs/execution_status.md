# Traffic Experiment — Execution Status Report

**Date:** 2026-08-03
**Branch:** `codex/claude-context-management-reconstruction`
**Machine:** 8× NVIDIA RTX 5880 Ada Generation (48 GB each), CUDA 13.0

---

## ✅ Completed

### 1. uv package manager installed (v0.12.1)
Replaces traditional `python3.11 -m venv` workflow. Automatically fetches Python 3.11 toolchains.

### 2. server.env created
Path: `traffic_experiment/server.env`

Key values filled:

| Variable | Value |
|---|---|
| `LOCOMO_DATA_DIR` | `/home/wongshingyin/commu/data/raw` |
| `CAPTURE_INTERFACE` | `lo` (loopback, since vLLM runs on 127.0.0.1) |
| `VLLM_HOST` | `127.0.0.1` |
| `VLLM_PORT` | `8000` |
| `VLLM_MODEL` | `Qwen/Qwen3-8B` |
| `CUDA_VISIBLE_DEVICES` | `0` |
| `TENSOR_PARALLEL_SIZE` | `1` |
| `COMPRESSOR_MODEL` | `NousResearch/Llama-2-7b-hf` |

LoCoMo data confirmed at `/home/wongshingyin/commu/data/raw/locomo10.json` (format: list of conversation+QA objects, compatible with the loader).

### 3. Python 3.11 venv created
- Path: `traffic_experiment/.venv-runner`
- Python: CPython 3.11.15 (fetched by uv)
- Runner deps installed: `httpx`, `PyYAML`
- Compression deps installed: `llmlingua==0.2.2`, `torch`, `transformers`, `nltk`, etc.

### 4. Scripts made executable
All `traffic_experiment/scripts/*.sh` now have `+x`.

---

## ⚠️ Blockers

### Blocker 1: wireshark CLI tools not installed
`dumpcap` and `tshark` are required for packet capture and analysis. They are not installed and need `sudo`:

```bash
sudo apt-get install -y wireshark-common tshark
```

After install, configure dumpcap for non-root capture (distribution-specific).

### Blocker 2: All 8 GPUs occupied by other users

| GPU | Memory Used | Utilization | Owner |
|---|---|---|---|
| 0–1 | 44.8 GB | 100% | VLLM instance (TP 0-1) |
| 2–3 | 44.8 GB | 100% | VLLM instance (TP 0-1) |
| 4–7 | 41.1 GB | 0% | VLLM instance, Qwen3.5-9B (TP 0-3) |

Additionally, port **8000** is already bound by a vLLM server running `Qwen3.5-9B` (user `mingyaliu8`).

This blocks:
- **LongLLMLingua compression** — needs GPU to load Llama-2-7B (~13 GB)
- **vLLM inference** — needs GPU(s) to load Qwen3-8B

### Blocker 3: NLTK security import (resolved)
`nltk` shipped with Python 3.11 blocks imports from CWD for security reasons.
The permanent repository workflow is now `bash scripts/run_tests.sh` from the
repository root. It runs from a temporary working directory, uses Python's `-P`
safe-path option, and never disables NLTK import security.

---

## 🔧 Workaround Options

| Option | Compression | vLLM Inference |
|---|---|---|
| **A: CPU compression + wait** | Set `COMPRESSOR_DEVICE=cpu` (slower) | Wait for a GPU to free, then start Qwen3-8B on a different port |
| **B: Reuse existing vLLM** | Set `COMPRESSOR_DEVICE=cpu` | Use existing Qwen3.5-9B on port 8000 (model differs from experiment spec) |
| **C: Wait for GPUs** | Use GPU as designed | Start fresh Qwen3-8B when GPU available |

---

## 📋 Remaining Steps (Happy Path)

```
1. [ ] sudo apt-get install wireshark-common tshark
2. [ ] Configure dumpcap non-root capture
3. [ ] Resolve GPU availability (wait or use CPU workaround)
4. [ ] Run 02_prepare_manifest.sh  → artifacts/requests_32.jsonl
5. [ ] Run 03_start_vllm.sh        → terminal 1 (background)
6. [ ] Run PROFILE=pilot ./scripts/run_local_experiment.sh  → terminal 2
7. [ ] Inspect pilot outputs in runs/local_vllm_pilot/
8. [ ] Run PROFILE=main ./scripts/run_local_experiment.sh   (~5–6 hours)
```

## 📊 Experiment Scale

| Profile | Samples | Conditions | Repetitions | Total Requests | Est. Wall Time |
|---|---|---|---|---|---|
| Pilot | 8 | 3 | 3 | 72 | ~0.7 h |
| Main | 32 | 3 | 5 | 480 | ~5–6 h |
| Robustness | 32 | 3 | 10 | 960 | ~10–11 h |

Conditions: `no_compression` (1×), `longllmlingua_2x` (0.5×), `longllmlingua_4x` (0.25×)
