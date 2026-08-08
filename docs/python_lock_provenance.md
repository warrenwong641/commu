# Python lock provenance

The committed Python locks support one explicit resolution target:

- operating system and architecture: glibc-based Linux x86_64;
- runtime feature version: CPython 3.11;
- resolver interpreter: CPython 3.11.15;
- resolver: uv 0.11.33;
- package source: the public PyPI simple index at
  `https://pypi.org/simple` only;
- release cutoff: `2026-08-08T00:00:00Z`;
- integrity: SHA-256 distribution hashes generated for every locked package.

`requirements.lock` is compiled from `requirements.txt` and
`requirements-test.txt`. `traffic_experiment/requirements-runner.lock` is
compiled from `traffic_experiment/requirements-runner.txt` and
`traffic_experiment/requirements-test.txt`.

Regenerate both files from the repository root:

```bash
bash scripts/compile_python_locks.sh
```

The script refuses a different uv version or host architecture, ignores user
index configuration, names the target platform and exact resolver Python patch,
pins the package index and release cutoff, and emits hashes. A clean
regeneration must leave the following command empty:

```bash
git diff -- requirements.lock traffic_experiment/requirements-runner.lock
```

The setup scripts require those hashes for both `uv pip sync` and the non-uv
`pip install` fallback. The resulting locks are not claimed to support Windows,
macOS, non-x86_64 systems, musl-based Linux, or a GPU/CUDA stack other than the
Linux wheels selected by this resolution. vLLM remains a separately managed
server environment and is not part of these locks.
