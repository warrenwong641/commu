# Qwen3.5-9B isolated vLLM environment

This directory specifies a separate environment for serving the already-cached
`Qwen/Qwen3.5-9B` snapshot at revision
`c202236235762e1c871ad0ccb60c8ee5ba337b9a`. It does not download the model,
load weights, start vLLM, contact the lab server, or modify the active service
scripts.

## Pinned software

| Component | Pin | Reason |
|---|---:|---|
| Python | CPython 3.12.13 | Exact isolated vLLM interpreter pin |
| vLLM | 0.26.0 | Stable release with native `Qwen3_5ForConditionalGeneration` support |
| PyTorch | 2.11.0 | Exact vLLM 0.26.0 CUDA dependency |
| Transformers | 5.5.3 | vLLM minimum; contains `qwen3_5` and `qwen3_5_text` config classes |
| tokenizers | 0.22.2 | Latest stable release inside Transformers 5.5.3's `>=0.22.0,<=0.23.0` bound; 0.23.0 was never published as a stable release |
| PyTorch CUDA wheel runtime | 12.9 | vLLM's default prebuilt CUDA runtime for this release |

Primary provenance:

- [vLLM 0.26.0 release](https://github.com/vllm-project/vllm/releases/tag/v0.26.0)
- [vLLM GPU installation requirements](https://docs.vllm.ai/en/stable/getting_started/installation/gpu/)
- [vLLM Qwen3.5 implementation](https://docs.vllm.ai/en/stable/api/vllm/model_executor/models/qwen3_5/)
- [vLLM supported-model text-only guidance](https://docs.vllm.ai/en/stable/models/supported_models/)
- [Transformers 5.5.3 Qwen3.5 documentation](https://huggingface.co/docs/transformers/v5.5.3/en/model_doc/qwen3_5)
- [PyTorch version archive](https://pytorch.org/get-started/previous-versions/)
- [NVIDIA CUDA compatibility documentation](https://docs.nvidia.com/deploy/cuda-compatibility/)

`requirements.lock` is the hash-checked resolver output for CPython 3.12.13 on
Linux x86-64 (`x86_64-manylinux_2_28`). Its resolver provenance, upload cutoff,
and isolated index policy are recorded in `LOCK_PROVENANCE.md`. Regenerate it
only with the scoped compiler:

```bash
cd traffic_experiment/environments/qwen35
UV_BIN=/absolute/path/to/uv-0.11.33 \
  ./compile_requirements.sh requirements.lock
```

Create the isolated environment on the target host only after its driver and
GPU have been inventoried:

```bash
cd /absolute/path/to/commu
UV_BIN=/absolute/path/to/uv-0.11.33
QWEN35_VENV=/absolute/path/outside/commu/.venv-vllm-qwen35
test "$("${UV_BIN}" --version)" = "uv 0.11.33 (x86_64-unknown-linux-gnu)"
"${UV_BIN}" venv --python 3.12.13 "${QWEN35_VENV}"
test "$("${QWEN35_VENV}/bin/python" --version)" = "Python 3.12.13"
CUDA_VISIBLE_DEVICES= "${UV_BIN}" pip sync \
  --python "${QWEN35_VENV}/bin/python" \
  --torch-backend cu129 \
  --require-hashes \
  traffic_experiment/environments/qwen35/requirements.lock
```

The environment directory and package caches must remain outside Git.

The dated lab inventory in `LAB_EVIDENCE.md` reports system Python 3.12.3 and
uv 0.12.1. Neither satisfies these exact pins, so the isolated environment is
not staged merely because those host tools exist.

The repository experiment runner intentionally uses its separate Python 3.11
environment (`.venv-runner`). It orchestrates experiments but does not host
vLLM. The Qwen3.5 model server intentionally uses this isolated CPython 3.12.13
environment; do not merge either environment or its dependency set into the
other.

## Static preflight

Point the checker at the cached snapshot directory or its `config.json`:

```bash
QWEN35_VENV=/absolute/path/outside/commu/.venv-vllm-qwen35
CUDA_VISIBLE_DEVICES= "${QWEN35_VENV}/bin/python" \
  traffic_experiment/environments/qwen35/preflight.py \
  /path/to/models--Qwen--Qwen3.5-9B/snapshots/c202236235762e1c871ad0ccb60c8ee5ba337b9a
```

The checker requires `CUDA_VISIBLE_DEVICES` to be present and exactly empty,
verifies every locked distribution plus the core pins, parses `torch/version.py`
as source text, and reads only `config.json`. It never imports the ML packages,
initializes CUDA, reads weights, or sends a request. A passing result means the
software pins and config identifiers match; it does **not** establish that the
lab driver, GPU, memory capacity, kernels, or multi-GPU topology work.

The resolved graph uses the CUDA 12.9 PyTorch wheel but also contains CUDA 13.x
build-tool/runtime components required by vLLM dependencies. Therefore the lock
must be treated as a whole during the live driver/kernel pilot; `cu129` in the
PyTorch version is not, by itself, a lab-driver compatibility claim.

Revision acceptance requires the lexical Hugging Face cache path
`models--Qwen--Qwen3.5-9B/snapshots/<revision>/config.json` and requires the
resolved config target to remain inside that model cache. This proves which
snapshot supplied the inspected config, not that every weight is complete.
The static checker never opens `model.safetensors-00001-of-00004` through
`model.safetensors-00004-of-00004`; their reported names and snapshot inventory
are recorded only as read-only operator evidence in `LAB_EVIDENCE.md`.

For text-only serving, retain the repository's existing `--language-model-only`
setting, `MAX_MODEL_LEN=65536`, and `MAX_OUTPUT_TOKENS=4096`. Pass the exact
model revision to vLLM, and use the cached snapshot; do not let a measured run
download files. Hardware validation is described in `HARDWARE_VALIDATION.md`.
