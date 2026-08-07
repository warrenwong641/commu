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

Install the Python dependencies in an isolated environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

Prepare LoCoMo data and run an experiment:

```bash
python -m locomo_eval.cli download-data
python -m locomo_eval.cli prepare
python -m locomo_eval.cli run --config configs/first_experiment.yaml
python -m locomo_eval.cli report --results-dir results/exp_002_stratified_budgeted
```

Run the test suite:

```bash
python -m pytest -q
```

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
