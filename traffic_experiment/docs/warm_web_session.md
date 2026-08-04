# Warm web-chat session model

## The architecture must be declared

Prompt compression changes wide-area traffic only when the client sends the
prompt or conversation history to the inference endpoint. Two common designs
must not be mixed:

1. **Stateless API chat.** The client resends the message history on every turn.
   Uplink bytes grow with the session, so compression can reduce WAN traffic.
2. **Stateful web application.** The browser sends a session identifier and the
   newest user message. A server-side application stores the history and builds
   the model prompt. Compression can reduce application-to-model traffic and
   model prefill work, but it does not substantially reduce browser uplink.

The public internals of consumer ChatGPT and Gemini websites are not the
experimental specification. Our reproducible analogue is HTTPS plus a streamed
SSE response, with one persistent client and sequential POST requests.

## Primary session schedule

Use a closed-loop schedule:

1. submit one frozen question on a fixed 60-second start schedule;
2. read the streamed response to completion;
3. remain silent for the rest of that minute;
4. submit the next question on the reused connection;
5. if a response exceeds 60 seconds, wait for completion rather than overlap it
   with the next request.

This follows Montieri et al.'s controlled workload: ten minutes, ten prompts,
one prompt per minute, and completion before the next prompt. It is a controlled
cadence rather than an empirical distribution of natural typing behavior.

A 30-second request-start interval may be reported as a higher-activity
sensitivity profile, but it is not the paper-matched condition. If a 30-minute
session is required, use 30 turns at the 60-second interval; do not describe that
longer session as a direct reproduction of Montieri et al.'s 10-minute session.

Capture the whole session in one pcap. Per-request 30-second captures would
insert artificial idle time between turns. Continue recording per-turn
application timestamps so each request can still be analyzed separately.

Run:

```bash
TRANSPORT=tls13 SESSION_TURNS=10 SESSION_START_INTERVAL_SECONDS=60 \
  scripts/14_run_warm_session.sh
```

Repeat with `TRANSPORT=http3`. The primary session uses one warm connection.
Cold setup remains a separate handshake-control experiment.

## Controlled network profiles

Keep three profiles:

- `baseline`: loopback/veth with no added impairment, to isolate the LLM;
- `rtt`: 40 ms RTT and no rate cap, to isolate propagation delay;
- `realistic`: 40 ms RTT, 20 Mbit/s uplink, and 50 Mbit/s downlink.

The numeric realistic profile is a declared experimental condition, not an
estimate of every Hong Kong-to-China link. Change it only through environment
variables and report the values.

After applying a profile, validate its achieved capacity rather than trusting
the configuration alone:

```bash
scripts/15_calibrate_link.sh
```

This runs separate iperf3 uplink and downlink transfers through the client
namespace and saves the JSON results. Run calibration outside the LLM capture
window so it does not contaminate the chat trace.

Text generation usually produces only hundreds or a few thousand bytes per
second, far below ordinary access-link capacity. Therefore downlink bandwidth
will rarely limit token delivery. Rate limits matter more for large prompt
uploads, while response latency is generally dominated by queueing, prompt
prefill, and token decoding.

## Server-side checker

The warm-session script snapshots vLLM's Prometheus endpoint immediately before
and after the session. It stores:

- token and request counter deltas;
- token and request rates over the session;
- queue/running/waiting metrics when exposed by the installed vLLM version;
- client-observed first-content delay and post-first-content token rate.

Do not label total-session token count divided by wall time as pure GPU decode
speed when the session contains think time. For model speed, use the per-request
post-first-content token rate. Use the server counter rate as a capacity and
consistency check.

## Recommended experiment matrix

For each transport and compression condition:

- one baseline session;
- three technical repetitions under the realistic profile;
- ten sequential turns per session;
- identical frozen prompt order;
- one-minute request-start interval;
- one persistent connection;
- one continuous packet capture and paired vLLM snapshots.

Run multiple concurrent sessions only in a separate capacity experiment. A
single-user interactive session and a server-saturation benchmark answer
different questions.
