# Conference-Rigor Checklist & Analysis Methodology

Prepared for the controlled vLLM/Qwen traffic-compression experiment.
Based on primary papers (Montieri et al.), ACM SIGCOMM/IMC artifact-evaluation
guidance, and preregistered design.

## Causal Controls

- Local vLLM + Qwen3-8B is the controlled baseline; external APIs (OpenRouter,
  Gemini) are validation environments only — provider infrastructure, routing,
  TLS records, and server-side behavior are not controlled.
- All backends use identical frozen prompts, system prompt, generation
  parameters (temperature=0, seed=42), and condition order.
- Compression performed once per sample, before measurement; repeated network
  trials reuse the exact compressed artifact.
- Each transport × network × workload block is independently configured,
  executed, and reported.

## Independent Unit

- The **sample** (not the repeated packet capture) is the independent unit.
  Technical repetitions are collapsed per sample before bootstrap.
- Disjoint complete sample blocks assigned to each GPU worker; port-specific
  capture filters prevent cross-worker packet attribution.
- GPU/worker identity recorded as a blocking factor; results never pooled
  without reporting the block.

## Repetitions & Blocking

- 3 technical repetitions per condition.  The pilot found identical repeated
  responses, median byte CV ≤ 0.26%, and median latency CV ≤ 0.7%, so three
  repetitions are sufficient for the primary run.
- Randomized trial order within each worker using fixed seed 42.
- GPU treated as fixed blocking factor.  Serial calibration subset runs on
  both GPUs to measure concurrency effects.

## Calibration

- iperf3 uplink/downlink throughput checked for every network condition
  (baseline, RTT, realistic) before LLM captures.  Calibration stored
  separately by condition; never mixed with experiment PCAPs.
- Capture overhead calibrated under the same recorded one- or two-worker load.
- Clock source (perf_counter / UTC), tool versions (tshark, dumpcap, vLLM,
  Caddy, kernel), and full command lines archived.

## Statistical Methodology

### Reporting

- Report **medians with interquartile range** (Q1, Q2, Q3), not means
  alone.
- **Paired comparisons** — the same sample appears in every compression
  condition and in both transport protocols.  Paired effect sizes reported.
- Report distributions: scatter plots, ECDF curves, and per-group
  direction breakdowns (upload/download kib).

### Confidence Intervals

- **Sample-level (clustered) bootstrap** 95% CIs: each bootstrap iteration
  resamples **samples** (not individual trials) with replacement, aggregates
  per-sample repetitions via median, then computes the statistic of interest.
  This correctly treats technical repetitions as nested within samples.
- 4,000 bootstrap iterations per estimate, seed 42.

### Multiple Comparisons

- Holm-Bonferroni correction applied to families of compression-condition
  comparisons (2x vs uncompressed, 4x vs uncompressed, 4x vs 2x) within
  each workload × transport group.
- All exclusions published; sensitivity analysis with failed trials
  retained as failures.

## Failure Reporting

- Failed trials logged without silent retries; rerun as separate documented
  trials in resumable append-only JSONL.
- `finish_reason` recorded per trial (stop vs length).  Length-capped rate
  explicitly reported.
- Transport failures, capture truncation, and error metadata preserved.
- `capture_may_be_truncated` field audited before any trial enters analysis.

## Protocol Verification

- Negotiated HTTP version verified per row: `1.1` for TLS/TCP, `3` for QUIC.
- QUIC run that used TCP or reports HTTP version ≠ 3 is **rejected** from
  the HTTP/3 analysis group.
- TLS 1.3 confirmed via Caddy configuration; no lower versions enabled.
- MTU 1500 confirmed; TSO/GSO/GRO/LRO disabled on veth endpoints.
- Warm-connection reuse demonstrated in captures before accepting warm
  profile.  Cold setup kept as separate handshake-overhead sensitivity.

## Reproducibility

- Model revision pinned to exact Hugging Face commit SHA (not branch).
- Manifest SHA-256 archived.
- Tool versions (tshark, dumpcap, vLLM, Caddy, kernel, CUDA driver) recorded
  in preflight audit.
- PCAP SHA-256 hashes recorded per trial.
- All non-secret configuration in a single `server.lab.env` file; credentials
  are injected only through the invoking process environment.
- Output token limit fixed at 4096; observed output token count recorded
  as covariate in per-token analyses.

## Primary vs Post-Hoc Sensitivity Separation

| Analysis | Status | Data Source |
|---|---|---|
| Per-request matrix (TLS + H3 × baseline/RTT/realistic × QA/summary) | **Primary** | JSONL + PCAPs |
| Warm 30-second closed-loop sessions | **Primary** | JSONL + PCAP + timeline CSVs |
| 10-minute Montieri-compatible sensitivity | **Post-hoc** | JSONL + PCAP |
| Cold-connection handshake overhead | **Post-hoc sensitivity** | Cold pilot |
| External API (OpenRouter / Gemini) | **Validation only** | JSONL + PCAPs |

- Never change preregistered settings after seeing primary results.
- Propose refinements only as **versioned secondary experiments** in a
  separate runs directory with documented rationale.

## Analysis Pipeline

Reproducible command:

```bash
python -m traffic_experiment.traffic_measure.cli report \
  --results runs/lab/local_vllm_tls13_main/results.jsonl \
  --output-dir runs/lab/analysis/primary_tls13 \
  --seed 42
```

### Output Files

| File | Content |
|---|---|
| `group_medians.csv` | Per-group median, IQR, bootstrap CI for all numeric metrics |
| `paired_ratios.csv` | Sample-level compressed/uncompressed ratios |
| `paired_ratio_summaries.csv` | Bootstrap CIs on paired compression ratios |
| `protocol_ratios.csv` | Sample-level HTTP/3 ÷ TLS 1.3 ratios |
| `scatter.csv` | Per-trial scatter data for plotting |
| `direction_medians.csv` | Upload/download breakdown by condition |
| `total_bytes_ecdf.csv` | ECDF of paired total-bytes ratios |
| `packet_size_summary.csv` | Per-PCAP packet-size distribution stats |
| `finish_reasons.csv` | Finish reason and failure counts |
| `REPORT_GENERATED` | Provenance marker |

### Metrics Computed per Group

- **Traffic**: packets total, bytes total, upload/download bytes, TCP/UDP
  payload, retransmissions, QUIC packets, protocol overhead ratio
- **Token efficiency**: bytes per input token, bytes per output token,
  upload/download separately
- **Latency**: elapsed seconds, TTFC, post-first-content token rate
- **Quality**: token F1, ROUGE-L (via locomo_eval)
- **Distribution**: packet-size min/median/mean/max, per-direction medians

### Missing (Data-Dependent)

The following require actual experiment PCAP data and cannot be tested
with synthetic fixtures:

- [ ] Per-PCAP packet-size distributions and protocol overhead ratios
- [ ] 1-second rate curves (requires continuous multi-second PCAP)
- [ ] GPU/worker blocking analysis (requires multi-worker result sets)
- [ ] Network calibration integration (requires iperf3 JSON output)
- [ ] Holm-corrected comparison tables (requires full matrix)
- [ ] Quality metrics (token F1, ROUGE-L) — require reference answers in
  manifest rows and locomo_eval loaded
