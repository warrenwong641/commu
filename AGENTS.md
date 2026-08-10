# Project Guidance

## Aim

This repository studies long-context management and the traffic effects of
prompt compression on LoCoMo workloads. Its connected workstreams are:

- `locomo_eval/`: compare compression and retrieval methods across context
  budgets using Qwen, with answer, evidence, perplexity, attention, latency, and
  GPU-memory metrics.
- `CLAUDE_CONTEXT_RECONSTRUCTION.md` and `locomo_eval/experimental/`: a
  clean-room, configurable baseline for Claude-Code-like context management.
- `traffic_experiment/`: measure frozen LoCoMo prompts through local vLLM and
  external validation backends over cleartext, TLS 1.3, and HTTP/3 paths.

Treat local vLLM as the controlled experiment server. External providers are
validation environments, not interchangeable model-compute replicates.

## Core Rules

- Freeze sample IDs, prompts, compression artifacts, model revisions,
  generation settings, seeds, and condition order before measured runs.
- Perform compression outside the measurement window and reuse the exact
  artifact for technical repetitions.
- Treat the sample, not each repeated capture, as the independent unit. Keep
  worker/GPU identity as a blocking factor.
- Do not silently retry or discard failed trials. Preserve error metadata and
  record reruns as separate attempts.
- vLLM is project infrastructure and may be configured, started, restarted, or
  stopped. Record existing ownership and configuration first; do not disturb
  unrelated GPU workloads or services.
- A public interface or port may be used when required. Record every
  project-created process, listener, proxy, tunnel, namespace, qdisc, container,
  firewall rule, and port mapping before activation.
- On completion or failure, remove all project-owned runtime and network setup,
  restore modified shared state, and verify that public ports are closed. Never
  stop an unrelated process merely because it uses a desired port.
- Keep API keys and tokens out of source, configs, logs, captures, and command
  output. Never give a sudo password to an agent or store one in an env file.

## Common Workflow

1. Read `README.md`, `traffic_experiment/README.md`, and the task-specific
   protocol or architecture document.
2. Inspect `git status`, the relevant diff, current server/GPU processes, and
   listener ownership before changing anything.
3. Make the smallest scoped change. Update docs, examples, schemas, and tests
   when behavior or experiment assumptions change.
4. Run focused tests, then the complete applicable suite:
   - `bash scripts/run_tests.sh`
   - `bash -n scripts/*.sh traffic_experiment/scripts/*.sh`
   - `shellcheck scripts/*.sh traffic_experiment/scripts/*.sh` when available.

   The wrapper runs the core and traffic suites explicitly from a temporary
   working directory with Python safe-path mode. Do not replace it with an
   unscoped repository-root `pytest` invocation.
5. For experiments: freeze manifests, run a small pilot, validate timestamps,
   token accounting, protocol negotiation, capture completeness, and traffic
   isolation, then run the main matrix.
6. Generate reports from immutable results and keep primary, validation, and
   post-hoc analyses separate.
7. Run the documented teardown for every project-created service and network
   change; verify process exit, listener closure, and firewall rollback.

## Common Tools

- `rg`, `git diff`, and `git status` for repository work.
- Python, pytest, YAML, pandas, PyTorch, Transformers, and sentence-transformers
  for LoCoMo evaluation.
- vLLM, `nvidia-smi`, `/proc`, `ps`, `ss`, and `lsof` for server/GPU lifecycle.
- `dumpcap`, `tshark`, `curl`, `iperf3`, `ip`, `tc`, and `ethtool` for protocol
  measurement and controlled links.
- Caddy for TLS 1.3/HTTP/3 termination; use the active firewall, namespace,
  container, or service manager only when the documented profile requires it.
- Persistent Linux sessions such as `tmux` for long server runs.

## Cautions

- Do not commit secrets, `server.env`, raw datasets, virtual environments,
  packet captures, run directories, generated bundles, or unreviewed reports.
- Pin model/tokenizer/provider revisions for measured work; recheck external
  prices, availability, and rate limits immediately before a final external run.
- Do not mix QA and event-summary results, warm and cold connections, local and
  external backends, or pilot and main results in one aggregate.
- Confirm TLS uses HTTP/1.1 and HTTP/3 uses QUIC with no TCP fallback before
  accepting a capture.
- Scope cleanup to recorded project-owned resources and verify exact targets
  before stopping, deleting, or changing firewall/network state.
- The current workflow is Linux/server-based. Do not use the legacy PowerShell
  client/tunnel path or a local Windows mock unless the user explicitly asks.
