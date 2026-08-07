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

Use a closed-loop session with a soft 30-second admission window:

1. start a warm connection and dispatch the first frozen prompt — the admission
   clock begins at this moment;
2. read its streamed response to completion;
3. if completion occurs before 30.000 seconds have elapsed, immediately admit
   the next frozen prompt;
4. repeat: after each completed response, if the admission clock is still under
   30.000 s, admit one more prompt — otherwise stop admitting;
5. the last admitted response is always allowed to finish past 30 seconds;
   never cancel an in-flight generation.

The 30-second budget controls whether a new request may start; it is not a
response timeout or truncation boundary. A session may contain many short
responses or a single response that exceeds the window. The number of admitted
prompts depends on the generation speed, not on a fixed count.

Montieri et al.'s paper-matched sensitivity profile remains available separately:
ten minutes, ten prompts, one prompt per minute, complete response before the
next prompt. Their protocol is controlled cadence, not an empirical distribution
of natural typing behavior.

Capture the whole session in one pcap. Per-request 30-second captures would
insert artificial idle time between turns. Continue recording per-turn
application timestamps so each request can still be analyzed separately.

Run:

```bash
TRANSPORT=tls13 SESSION_CONDITION=no_compression \
  SESSION_TURNS=2 SESSION_BUDGET_SECONDS=30 \
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
- identical frozen prompt order;
- one compression condition per session so every sample under that condition
  is eligible for admission;
- a 30-second soft admission window: prompts are admitted sequentially until
  the 30-second clock expires; the last admitted response always finishes;
- one persistent connection;
- one continuous packet capture and paired vLLM snapshots.

Run multiple concurrent sessions only in a separate capacity experiment. A
single-user interactive session and a server-saturation benchmark answer
different questions.
