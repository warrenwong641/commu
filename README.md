# commu

`commu` is a research repository for evaluating long-context management and
measuring how prompt compression changes LLM network traffic.

The benchmark workload is
[LoCoMo](https://github.com/snap-research/locomo), a long-conversation memory
dataset. The primary controlled model path uses Qwen served locally; external
APIs are treated as validation environments.

## Repository map

- `locomo_eval/`: LoCoMo loading, formatting, compression methods, model
  adapters, metrics, experiment runners, and report generation.
- `configs/`: reproducible context-budget and compression experiment settings.
- `tests/`: unit and integration tests for the core evaluation framework.
- `CLAUDE_CONTEXT_RECONSTRUCTION.md`: clean-room Claude-Code-like context
  management baseline and its documented limits.
- `traffic_experiment/`: frozen-manifest traffic experiments using local vLLM,
  OpenRouter, and Gemini across cleartext HTTP, TLS 1.3, and HTTP/3.
- `server_side_zero_transport/`: a loopback-only scaffold for validating an
  already-running two-GPU vLLM service without changing it.
- `results/`: intentionally curated result and report artifacts.

## Core evaluation

The supported development runtime is Python 3.11. Install the locked core and
test dependencies with `uv`:

```bash
bash scripts/setup_python.sh
. .venv/bin/activate
```

`uv` is the portable primary path because it can obtain Python 3.11 and
synchronizes the exact versions and hashes in `requirements.lock`. If `uv` is
unavailable, the same script can use an existing CPython 3.11 interpreter,
`venv`, and its bundled pip without upgrading it; package names and availability
vary by distribution and release, so set `PYTHON_BIN` to the installed
interpreter. Regenerate all locks
using the pinned resolver workflow:

```bash
bash scripts/compile_python_locks.sh
```

The supported lock target and complete resolver provenance are documented in
[`docs/python_lock_provenance.md`](docs/python_lock_provenance.md). These locks
are not claimed to support Windows, macOS, other CPU architectures, or musl.

Prepare LoCoMo data and run an experiment:

```bash
python -m locomo_eval.cli download-data
python -m locomo_eval.cli prepare
python -m locomo_eval.cli run --config configs/first_experiment.yaml
python -m locomo_eval.cli report --results-dir results/exp_002_stratified_budgeted
```

Run both the core and traffic test suites:

```bash
bash traffic_experiment/scripts/01_setup_runner.sh
bash scripts/run_tests.sh
```

The traffic setup creates `.venv-runner` from its runtime/test lock and a
separate `.venv-compression` from its manifest-preparation lock. Compression
packages never mutate the runner environment.

The wrapper invokes both interpreters with Python's `-P` safe-path option from
a temporary working directory. It clears inherited Python import/install
variables, sets only the repository import root, and passes only `tests/` and
`traffic_experiment/tests/` to pytest. `pytest.ini` excludes runtime trees from
accidental discovery without excluding tracked environment specifications.

The core configuration compares no compression, recent-turn windows, sparse and
dense retrieval, neighbor windows, hybrid selection, and oracle evidence across
fixed or full token budgets.

## Traffic experiment

Read [`traffic_experiment/README.md`](traffic_experiment/README.md) before
running network measurements. It defines the controlled vLLM baseline, frozen
prompt manifests, pilot and main profiles, packet-capture requirements, physical
client validation, statistical reporting, and teardown procedures.

The vLLM service is the experiment server and may be configured or restarted for
the project. Any project-created vLLM process, public listener, proxy, tunnel,
namespace, traffic-control rule, firewall rule, or port mapping must be recorded
and removed after the run. Do not modify or stop unrelated services.

## Data and reproducibility

- Raw and processed LoCoMo datasets remain outside version control.
- Compression happens before packet capture; repetitions reuse byte-identical
  prompt artifacts.
- Model and tokenizer revisions, seeds, manifests, tool versions, timestamps,
  GPU identity, and capture hashes are recorded for measured runs.
- API keys stay only in the invoking process environment and are never written
  to project configuration or committed.
- Packet captures and run directories remain outside version control unless a
  small, reviewed artifact is intentionally curated for publication.
