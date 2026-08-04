# Controlled network conditions

## Relationship to the Montieri et al. setup

Montieri et al., *From Prompts to Packets: A View from the Network on ChatGPT,
Copilot, and Gemini*, capture official Android-app traffic at the Wi-Fi access
point. The paper does not explicitly report an MTU. Its released controlled
dataset contains ChatGPT IP packets up to 1500 bytes and Gemini packets up to
1452 bytes, so this experiment uses an MTU of 1500 bytes.

The paper issues ten prompts within one ten-minute app session. Its released
controlled traces contain major ChatGPT and Gemini flows lasting approximately
662 and 555 seconds, respectively. We therefore treat a reused, warm connection
as the primary app-like condition. The existing cold-connection pilot remains a
separate measurement of connection-setup overhead.

Paper: <https://arxiv.org/abs/2510.11269>

## Link definitions

Both conditions use:

- a client network namespace connected to the host by a veth pair;
- MTU 1500 on both ends;
- TSO, GSO, GRO, and LRO disabled;
- no packet loss or rate limit;
- capture on the client-side veth interface;
- the same prompts, model, generation settings, and randomized trial order.

The two conditions are:

1. `baseline`: no added delay.
2. `rtt`: 20 ms one-way delay on each veth egress, producing approximately
   40 ms round-trip time.

Keeping loss at zero isolates the effect of propagation delay. Loss can be added
later as a separately declared robustness experiment.

## Apply a condition

Run as root:

```bash
scripts/11_network_condition.sh apply baseline
scripts/11_network_condition.sh status
```

For the realistic-delay condition:

```bash
scripts/11_network_condition.sh reset
scripts/11_network_condition.sh apply rtt
```

Before starting Caddy and the traffic runner, set:

```bash
SECURE_PROXY_HOST=10.200.0.1
CLIENT_NETNS=llm-client
CAPTURE_INTERFACE_OVERRIDE=llmclient0
```

Restart Caddy after changing `SECURE_PROXY_HOST`, because its locally issued
certificate must include the measured address. The runner and `dumpcap` execute
inside the client namespace while Caddy and vLLM remain in the host namespace.

Use `scripts/11_network_condition.sh reset` only for the exact experiment
namespace and interfaces created by the script.

## Warm versus cold reporting

Do not pool warm and cold results:

- warm: establish one connection per worker before measured requests, reuse it
  across that worker's randomized trial sequence, and exclude the initial
  handshake from per-request captures;
- cold: establish a new TLS or QUIC connection for every request and include its
  setup traffic.

The primary full experiment should use warm connections. The completed cold pilot
is retained as a sensitivity result, so no extra ten-repetition experiment is
required.
