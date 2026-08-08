# Read-only lab evidence

This record captures evidence supplied on 2026-08-08. It does not record a
pilot, a deployment, or permission to inspect or change the lab host. No model
weights were read to produce or validate this repository change.

## Observed host and hardware

- Host: Ubuntu 24.04.2 with glibc 2.39. This is newer than the lock's
  `x86_64-manylinux_2_28` glibc 2.28 floor.
- Driver: NVIDIA 580.82.07; `nvidia-smi` reports CUDA 13.0.
- Allowed devices: physical GPUs 0 and 1 are RTX 5880 Ada devices. Each has
  compute capability 8.9 and 49,140 MiB. Both are currently occupied by the
  project vLLM service.
- Forbidden devices: GPUs 2 and 3 are free but remain prohibited by project
  policy. GPUs 4-7 host unrelated vLLM workloads and must never be touched.
- Storage: approximately 750 GiB is free under `/home`, despite the containing
  filesystem reporting 98% use. Free capacity is not proof of a successful
  install or model load.

NVIDIA's compatibility table places CUDA 12.9 below driver 580.82.07, and gives
580.65.06 as the CUDA 13.0 GA minimum. Therefore the selected PyTorch `cu129`
runtime is not statically blocked by the observed driver. This is only a
version-table result: the resolved environment also contains CUDA 13.x
components, and kernel execution, dtype support, memory fit, and multi-GPU
behavior remain pending until an authorized disposable live pilot.

## Observed cache and tools

The pinned snapshot was reported at the canonical lexical path:

```text
.../models--Qwen--Qwen3.5-9B/snapshots/c202236235762e1c871ad0ccb60c8ee5ba337b9a/
```

It contains 16 entries, including exactly these four weight-shard names:

```text
model.safetensors-00001-of-00004
model.safetensors-00002-of-00004
model.safetensors-00003-of-00004
model.safetensors-00004-of-00004
```

Zero broken symlinks were reported. This inventory supports snapshot presence
but does not prove file contents, hashes, completeness, or loadability. The
static preflight continues to read only `config.json`, not weight shards.

The host system Python is 3.12.3, not the required isolated CPython 3.12.13.
The available `~/.local/bin/uv` is 0.12.1, not the required resolver uv 0.11.33.
Consequently the isolated environment and exact compiler are **not staged**,
and deployment readiness remains non-green. `compile_requirements.sh` requires
an explicit or path-resolved uv 0.11.33 x86-64 Linux binary and fails closed for
the observed uv 0.12.1.

## Current conclusion

Static evidence supports the OS/glibc floor, the GPU compute-capability floor,
the pinned snapshot's lexical layout, and the absence of a driver-version block
for `cu129`. It does not establish exact tool staging or live runtime fitness.
Do not replace or disturb the active project service until the remaining steps
in `HARDWARE_VALIDATION.md` are authorized and pass.
