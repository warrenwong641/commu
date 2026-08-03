# Secure transport, summary, and API profiles

## Prepare event summaries

Run `scripts/02_prepare_summary_manifest.sh`. It selects conversations with
annotated event summaries, creates a speaker-specific chronological-summary prompt,
and precomputes the same three compression conditions used by QA. Point
`MANIFEST_PATH` at the resulting file and set `MAX_OUTPUT_TOKENS=512` when measuring
this workload.

## Start the local secure proxy

Run `scripts/00_install_caddy.sh` to install the pinned, checksum-verified Caddy
binary inside the experiment directory, then run `scripts/07_start_secure_proxy.sh`.
The script validates `configs/Caddyfile`, starts Caddy in the background, and prints
the local CA path. Keep vLLM on port 8000.

- TCP 8443: TLS 1.3 plus HTTP/1.1.
- UDP 8444: TLS 1.3 as used by QUIC plus HTTP/3 only.
- TCP/UDP 8543/8544: identical transports routed only to the second vLLM worker.

The runner uses aioquic for an HTTP/3-only client, so there is no TCP fallback.
Run `TRANSPORT=tls13 scripts/08_run_transport_profile.sh` and then
`TRANSPORT=http3 scripts/08_run_transport_profile.sh`. Analyze each output
directory separately. A valid QUIC row must record HTTP version 3 and the capture
must contain UDP/QUIC packets on port 8444.

For the two-GPU pilot, use `scripts/08_run_transport_profile_parallel.sh`.
It assigns disjoint complete sample blocks to ports 8000 and 8001 through separate
secure listeners, merges the 72 rows, and runs tshark analysis automatically.

## External API pilot

Fill the OpenRouter or Gemini variables in the untracked `server.env`. OpenRouter
requires both an exact model slug and a single provider name; the request disables
fallback. Gemini requires a stable model ID. Then run:

```bash
BACKEND=openrouter PROFILE=pilot scripts/09_run_api_profile.sh
BACKEND=gemini PROFILE=pilot scripts/09_run_api_profile.sh
```

API keys are read from environment variables and are not written into results.
The runner stores returned usage, provider/generation ID, and model version when
the service exposes them. External TLS captures remain encrypted; packet size,
direction, timing, retransmission, and endpoint metadata are sufficient for the
traffic analysis.

## Required validity checks

1. Keep QA and event-summary analyses separate.
2. Confirm the negotiated HTTP version in every successful transport row.
3. Reject any QUIC run that used TCP or reports a version other than HTTP/3.
4. Inspect a capture for unrelated traffic before the pilot.
5. Report cold and warm connection results separately.
6. Pin dataset, manifest hash, model revision, provider route, commands, and tool
   versions before the main experiment.
