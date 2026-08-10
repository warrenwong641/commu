# Lock provenance

- Compiler: `uv 0.11.33 (x86_64-unknown-linux-gnu)`
- Interpreter target: `CPython 3.12.13`
- Platform target: Linux x86-64, `x86_64-manylinux_2_28`
- General package index: `https://pypi.org/simple`
- PyTorch wheel routing: uv `--torch-backend cu129`
- Release cutoff: `2026-08-08T12:00:00Z` (before the original commit at
  `2026-08-08T12:15:03Z`)
- Integrity: hashes are mandatory through `--generate-hashes`
- Configuration isolation: uv `--no-config`; inherited uv/pip index, find-link,
  cutoff, backend, Python-path, virtual-environment, and Conda variables are
  cleared by `compile_requirements.sh`.

Exact deterministic compile command recorded in the lock header:

```bash
CUDA_VISIBLE_DEVICES= uv --no-config pip compile --python-version 3.12.13 --python-platform x86_64-manylinux_2_28 --default-index https://pypi.org/simple --torch-backend cu129 --exclude-newer 2026-08-08T12:00:00Z --generate-hashes --emit-index-url requirements.in -o requirements.lock
```

From this directory, run `./compile_requirements.sh requirements.lock`. For a
reproducibility check, compile twice to two temporary paths and require both
`cmp` and SHA-256 to match. Cache location may be relocated to a writable
directory; cache contents and location are not resolver inputs.
