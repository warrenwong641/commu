# Python lock provenance

The committed Python locks support one explicit resolution target:

- operating system and architecture: glibc-based Linux x86_64;
- runtime feature version: CPython 3.11;
- resolver interpreter: CPython 3.11.15;
- resolver: uv 0.11.33;
- package sources: the public PyPI simple index at `https://pypi.org/simple`
  plus the single SHA-256-pinned official PyTorch CPU wheel direct URL at
  `https://download-r2.pytorch.org/whl/cpu/torch-2.7.1%2Bcpu-cp311-cp311-manylinux_2_28_x86_64.whl`;
- index policy: PyPI is the only package index; the PyTorch wheel is one pinned
  direct artifact, not a general secondary index;
- release cutoff: `2026-08-08T00:00:00Z`;
- integrity: SHA-256 distribution hashes generated for every locked package.

`requirements.lock` is compiled from `requirements.txt` and
`requirements-test.txt`. `traffic_experiment/requirements-runner.lock` is
compiled from `traffic_experiment/requirements-runner.txt` and
`traffic_experiment/requirements-test.txt`.
`traffic_experiment/requirements-compression.lock` is compiled from
`traffic_experiment/requirements-runner.txt` and
`traffic_experiment/requirements-compression.txt`; it provides an isolated
CPU-only manifest-preparation environment and is never synchronized into the
runner or vLLM environments. Its PyTorch input is the CPython 3.11/Linux x86_64
`torch 2.7.1+cpu` wheel from PyTorch's official CPU wheel repository, pinned by
its SHA-256 hash. The lock intentionally contains no CUDA toolkit, NVIDIA
runtime, or Triton packages.

Regenerate all three files from the repository root:

```bash
bash scripts/compile_python_locks.sh
```

The script refuses a different uv version or host architecture, ignores user
index configuration, names the target platform and exact resolver Python patch,
pins the package index and release cutoff, and emits hashes. A clean
regeneration must leave the following command empty:

```bash
git diff -- requirements.lock \
  traffic_experiment/requirements-runner.lock \
  traffic_experiment/requirements-compression.lock
```

The setup scripts require those hashes for both `uv pip sync` and the non-uv
`pip install` fallback. The fallback uses the pip bundled by Python's `venv`
module, verifies that it supports `--require-hashes`, and performs no network
upgrade or other install before synchronizing the lock. The resulting locks are
not claimed to support Windows, macOS, non-x86_64 systems, musl-based Linux, or
GPU compression. A GPU compressor requires a separate environment plus a
separately approved compatibility smoke test before it may be selected. The
approval must record the exact interpreter path in
`GPU_COMPRESSOR_APPROVED_PYTHON`, and that path must resolve to the selected
`COMPRESSION_PYTHON`; this CPU lock is not evidence for any CUDA configuration.
vLLM remains a separately managed server environment and is not part of these
locks.
