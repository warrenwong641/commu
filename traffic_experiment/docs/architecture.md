# Measurement Architecture

## Local controlled path

```text
LoCoMo runner container
        |
        | OpenAI-compatible HTTP/SSE
        v
isolated bridge or veth pair ---- dumpcap on the bridge ---- vLLM container
                                                        Qwen/Qwen3-8B
```

Run the client and vLLM server in separate containers or network namespaces.
Capture on their dedicated bridge or veth interface. This prevents unrelated lab
traffic from entering the measurement and makes client-to-server direction explicit.

vLLM exposes an OpenAI-compatible `/v1/chat/completions` endpoint. Streaming uses
server-sent events (SSE) over HTTP, normally carried by TCP. This lets the same
logical request interface drive the local and OpenRouter backends.

## External API path

```text
LoCoMo runner container
        |
        | HTTPS/TLS through dedicated egress interface
        v
dumpcap on client egress ---- Internet ---- OpenRouter or Gemini
```

For external services, packet capture reveals packet sizes, directions, timing,
TCP/TLS overhead, retransmissions, and destination endpoints. It does not reveal
encrypted prompt or response content unless TLS session keys are deliberately
logged. Payload decryption is unnecessary for the intended byte and timing metrics.

OpenRouter and Gemini are separate experimental environments. Do not present their
traffic as a direct measurement of model computation: it also includes provider
routing, front-end, TLS, and streaming implementation.

## Components

### Backend-neutral runner

The runner should accept a frozen request manifest and write one JSONL result row
per call. It is responsible for:

- generating a unique run ID;
- recording monotonic and UTC timestamps;
- issuing a single request;
- recording first-byte, first-token, final-token, and completion timestamps;
- recording backend token usage where available;
- saving the response and error metadata outside the packet capture.

### Capture controller

Use `dumpcap` for acquisition because it is lightweight and can run with narrow
capture privileges. Use `tshark` afterward to calculate:

- total captured bytes and packets;
- client-to-server and server-to-client bytes;
- TCP payload bytes;
- duration, time to first byte, burst/gap statistics;
- retransmissions and connection setup overhead.

The capture process should start before the request and stop after the fixed
30-second observation window. Save the capture command, interface name, filter,
tool version, and SHA-256 hash in the run metadata.

## Warm and cold connections

Use warm connections as the primary condition because they reduce DNS, TCP, and TLS
setup noise. Run cold connections as a smaller secondary analysis when connection
setup overhead itself is relevant. Never combine warm and cold results in one mean.

## Traffic isolation

- Run one measured request at a time.
- Disable automatic retries in the primary experiment; record failures and rerun
  them as separate trials.
- Pause background model downloads and system updates.
- Resolve and record actual destination IPs immediately before each external run.
- Prefer a dedicated egress namespace or VM when measuring an external service.
- Pin the OpenRouter provider and disable fallback so a model request is not silently
  routed to a different serving stack.
