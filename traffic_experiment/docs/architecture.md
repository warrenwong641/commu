# Measurement Architecture

## Local controlled path

```text
LoCoMo runner container
        |
        | OpenAI-compatible HTTP/SSE
        v
isolated bridge or veth pair ---- dumpcap on the bridge ---- vLLM container
                                                        Qwen/Qwen3.5-9B
```

Run the client and vLLM server in separate containers or network namespaces.
Capture on their dedicated bridge or veth interface. This prevents unrelated lab
traffic from entering the measurement and makes client-to-server direction explicit.

vLLM exposes an OpenAI-compatible `/v1/chat/completions` endpoint. For secure
transport profiles, Caddy terminates TLS 1.3 and forwards the unchanged request to
vLLM. Port 8443 accepts HTTP/1.1 over TLS/TCP; port 8444 accepts HTTP/3 over QUIC/UDP.
The cleartext control remains on port 8000.

For the two-GPU local profile, two independent vLLM processes listen on ports
8000 and 8001. Each runner receives a disjoint deterministic set of complete
sample blocks, and each dumpcap filter selects only its worker's TCP port. Requests
remain serial within a worker; concurrency exists only across isolated workers.
GPU/worker identity is recorded and treated as a blocking factor in analysis.

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
- recording workload, backend, transport, connection mode, negotiated HTTP version,
  provider/model response identifiers, and returned usage metadata.

### Capture controller

Use `dumpcap` for acquisition because it is lightweight and can run with narrow
capture privileges. Use `tshark` afterward to calculate:

- total captured bytes and packets;
- client-to-server and server-to-client bytes;
- TCP payload bytes;
- duration, time to first byte, burst/gap statistics;
- retransmissions and connection setup overhead.
- UDP payload, QUIC packet counts, observed TLS record versions, and ALPN values
  where tshark can decode them without session secrets.

The capture process should start before the request and stop after the fixed
30-second observation window. Save the capture command, interface name, filter,
tool version, and SHA-256 hash in the run metadata.

## Warm and cold connections

The HTTPX cleartext/API path and strict TLS 1.3 path can reuse a warm connection.
The HTTP/3 path keeps one aioquic connection open per worker and uses a new stream
for each request. For strict TLS, cold mode uses an external curl process for each
request; for HTTP/3, cold mode creates a new QUIC connection for each request.
Never combine warm and cold results in one mean. Demonstrate connection reuse in
the captures before accepting a warm profile.

## Traffic isolation

- Run one measured request at a time.
- Disable automatic retries in the primary experiment; record failures and rerun
  them as separate trials.
- Pause background model downloads and system updates.
- Resolve and record actual destination IPs immediately before each external run.
- Prefer a dedicated egress namespace or VM when measuring an external service.
- Pin the OpenRouter provider and disable fallback so a model request is not silently
  routed to a different serving stack.
