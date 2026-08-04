# Hong Kong client to AutoDL model-server protocol

## What this experiment answers

This profile measures the real path from the Windows client in Hong Kong to the
Qwen server hosted by AutoDL in mainland China. It is deliberately separate from
the server-internal TLS/QUIC experiment.

Use two complementary measurements:

1. a network-only calibration transfer, to estimate path latency and bulk
   goodput without model computation; and
2. the frozen LoCoMo request stream, to measure response-header delay,
   first-content delay, content-event spacing, tokens per second, and request and
   response application bytes.

Do not describe response bytes divided by total LLM latency as "download
bandwidth." The model generates tokens much more slowly than the network can
usually carry them.

## Access options and limitations

### Immediate private option: SSH local forwarding

Copy the instance's SSH host and port from the AutoDL console. Run this command
on the Windows client, substituting those two values:

```powershell
ssh -N -L 18000:127.0.0.1:8000 root@SSH_HOST -p SSH_PORT
```

Do **not** add `-C`. SSH compression would alter the byte count and timing that
the experiment is intended to observe. Keep the tunnel terminal open, then run:

```powershell
.\traffic_experiment\scripts\run_remote_client.ps1
```

The tunnel measures the geographic Hong Kong-to-China path and model streaming,
but it is TCP inside SSH. It therefore cannot be used as the final direct
TLS-versus-QUIC comparison.

The remote client uses the same soft 30-second session admission rule as the
controlled testbed: it finishes an in-flight response, admits a second prompt
only when the first finishes before 30 seconds, and then ends the warm session.

### Final public option

AutoDL instances do not have an independent public IP. Their documented custom
service maps instance ports 6006 and 6008 to a public TCP or HTTP address and
requires account verification. Bind the authenticated model gateway to one of
those ports only after access control is enabled.

The documented mapping does not expose UDP. Consequently, direct client-to-
server HTTP/3/QUIC cannot be evaluated through this AutoDL service. A final
TLS-versus-QUIC wide-area experiment requires a host or relay with a public UDP
port, or a different cloud instance with a public IP. Report the SSH/custom-
service result as a real-path TCP validation, not as the QUIC result.

## Required measurements

For every frozen prompt and compression condition, record:

- request JSON bytes and input tokens;
- response SSE bytes and output tokens;
- response-header and first-content delay;
- end-to-end latency;
- content event count and inter-event p50/p95;
- post-first-content output tokens per second;
- connection mode, endpoint, timestamp, and repetition;
- client-side packet capture on the physical interface when permitted.

Keep the existing 30-second packet-observation window for comparability, but
also preserve the complete application transaction. Mark transactions exceeding
30 seconds rather than truncating their latency.

## Interpretation

More prompt compression should reduce uplink bytes and may reduce model prefill
and first-content delay. It does not imply a higher physical downlink rate or
faster token generation. Output tokens per second is primarily model-bound; the
network is implicated when inter-content gaps, retransmissions, or first-byte
delay rise while server-side generation timing remains stable.

Run the same frozen requests in blocks that rotate conditions over time. This
reduces bias from shared-bandwidth load changes. Use at least three repetitions
for the presentation-quality result and report medians with per-sample paired
differences.

## QA quality audit

Historical `token_f1` used whitespace-only tokenization, so punctuation and
articles produced avoidable false penalties. The corrected metric applies
SQuAD-style normalization and preserves the old value as `legacy_token_f1`.

The remaining low scores are not only a metric artifact. Manual review found
semantic paraphrases and verbose-but-correct answers, but also genuine missing
facts. Several 4x-compressed prompts removed the evidence needed to answer the
question. Therefore report normalized token F1 and stemmed ROUGE-L, plus a
paired evidence-preservation analysis by sample and compression ratio.
