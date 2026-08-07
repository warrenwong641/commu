# Cost and Time Estimates

Pricing was checked on 2026-08-03. Recheck it immediately before execution.
All amounts below are USD and exclude taxes.

## Assumptions

- Three equally represented prompt conditions: 9,000 tokens uncompressed,
  approximately 4,500 tokens at 2x, and approximately 2,250 tokens at 4x.
- Weighted average input: `(9000 + 4500 + 2250) / 3 = 5,250` tokens/call.
- Expected output: 128 tokens/call, with a configured maximum of 256.
- Calls are serial.
- Each call reserves 30 seconds of observation plus 5 seconds of setup/teardown.
- Compression is precomputed and reused across repetitions.

Actual provider billing must be calculated from returned usage fields, because
tokenizers and achieved compression ratios may differ from these assumptions.

## Token and wall-time budget

| Profile | Calls/backend | Input tokens | Output tokens | Capture wall time/backend |
|---|---:|---:|---:|---:|
| Pilot | 72 | 378,000 | 9,216 | 0.70 h |
| Main | 480 | 2,520,000 | 61,440 | 4.67 h |
| Robustness | 960 | 5,040,000 | 122,880 | 9.33 h |

The fixed capture protocol, not inference latency, sets the lower-bound wall time.
Rate limiting, failures, model loading, cooldowns, and validation add time. Budget
approximately 1 hour/backend for the pilot, 6 hours/backend for the main profile,
and 11 hours/backend for the robustness profile.

Running all three backends serially therefore requires approximately 3 hours for
the pilots or 18 hours for the conservatively budgeted main profile, including
setup and validation. The configured capture-and-teardown intervals alone total
2.1 hours and 14 hours, respectively.

## OpenRouter: Qwen3-8B

Assumed listed inference price:

- input: $0.05 per million tokens;
- output: $0.40 per million tokens.

Formula:

`cost = input_millions * 0.05 + output_millions * 0.40`

| Profile | Estimated inference usage |
|---|---:|
| Pilot | $0.023 |
| Main | $0.151 |
| Robustness | $0.301 |

These very small usage values are not the same as cash required to open an account.
OpenRouter documents a 5.5% fee when purchasing credits, a minimum purchase, and a
minimum fee; confirm the checkout total rather than treating the fractions above as
the upfront budget. Pin a provider advertising the assumed rate or update the YAML
price before running.

## Direct Gemini API

Recommended cost-focused validation model: Gemini 3.5 Flash-Lite.

Assumed standard paid-tier price:

- input: $0.30 per million tokens;
- output: $2.50 per million tokens.

| Profile | Gemini 3.5 Flash-Lite |
|---|---:|
| Pilot | $0.136 |
| Main | $0.910 |
| Robustness | $1.819 |

For a quality-focused reference, Gemini 3.5 Flash was listed at $1.50/M input and
$9.00/M output:

| Profile | Gemini 3.5 Flash reference |
|---|---:|
| Pilot | $0.650 |
| Main | $4.333 |
| Robustness | $8.666 |

The free tier may reduce direct charges, but it has different limits and data-use
terms. For a reproducible research run, record whether the project is on the free
or paid tier and inspect the active RPM, TPM, and daily quota in AI Studio.

## Local Qwen3-8B via vLLM

There is no per-token API fee on lab-owned hardware. The estimate below assumes:

- 350 W GPU;
- 100 W for the rest of the host;
- 0.45 kW total during the reserved experiment;
- electricity at $0.15/kWh.

Formula:

`energy cost = wall_hours * 0.45 kW * $0.15/kWh`

| Profile | Reserved wall time | Conservative energy cost |
|---|---:|---:|
| Pilot | 0.70 h | $0.047 |
| Main | 4.67 h | $0.315 |
| Robustness | 9.33 h | $0.630 |

This is conservative because the GPU may be partly idle during the remainder of
each 30-second observation window. If the server would be powered anyway, the
incremental cost is lower.

If a GPU must be rented, insert the actual provider quote. An illustrative
$0.50-$1.50/GPU-hour range gives:

| Profile | Illustrative rental range |
|---|---:|
| Pilot | $0.35-$1.05 |
| Main | $2.33-$7.00 |
| Robustness | $4.67-$14.00 |

This rental range is a planning assumption, not a current vendor quote.

## Compression time

The compressor runs only for the two compressed conditions:

- pilot: 8 samples x 2 ratios = 16 unique compression jobs;
- full 32-sample set: 32 x 2 = 64 unique compression jobs.

Do not guess the LongLLMLingua runtime for the lab GPU. Benchmark five jobs, calculate
the median seconds/job, and estimate:

`compression_time = unique_jobs * median_seconds_per_job`

Add model load time once. The measured compression runtime and its hardware/software
configuration should be reported separately from network inference time.

## Sources

- [OpenRouter Qwen3-8B pricing](https://openrouter.ai/qwen/qwen3-8b/pricing)
- [OpenRouter pricing and platform fee](https://openrouter.ai/pricing)
- [OpenRouter FAQ](https://openrouter.ai/docs/faq)
- [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing)
- [Gemini API rate limits](https://ai.google.dev/gemini-api/docs/rate-limits)
- [Gemini model identifiers](https://ai.google.dev/gemini-api/docs/models)
- [vLLM OpenAI-compatible server](https://docs.vllm.ai/en/latest/serving/openai_compatible_server/)
