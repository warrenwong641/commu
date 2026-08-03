# Run Protocol

## Phase 0: freeze inputs

1. Select 32 LoCoMo sample IDs with a fixed seed and write them to a manifest.
2. Freeze the system prompt, question, expected answer, and generation settings.
3. Produce three prompt artifacts per sample: no compression, LongLLMLingua 2x,
   and LongLLMLingua 4x.
4. Record the compressor model/revision, parameters, actual token count, execution
   time, and artifact hash.
5. Manually inspect at least five samples for answer-bearing information lost during
   compression.

Compression is performed once per sample and ratio, before packet capture. Repeated
network trials reuse the exact artifact.

## Phase 1: validate the local path

1. Pin the Qwen3-8B model revision and vLLM version.
2. Start vLLM and perform unmeasured warm-up calls until latency stabilizes.
3. Verify that the client reaches vLLM only across the measured interface.
4. Run one request and confirm that the application timestamps fall inside the
   packet-capture timestamps.
5. Compare application request/response byte counts with TCP/TLS capture totals.

## Phase 2: pilot

Run 8 samples x 3 conditions x 3 repetitions = 72 calls per backend.

- Randomize trial order within each backend using the fixed seed.
- Keep backends in separate blocks; never overlap captures.
- Use a 30-second observation window and approximately 5 seconds for setup/teardown.
- Log failures without silently retrying.
- Check token counts, captures, and result completeness before moving forward.

Pilot exit criteria:

- all required manifest fields are present;
- at least 95% of trials complete without transport failure;
- every successful trial has a nonempty capture;
- no unrelated traffic appears in a manual packet inspection;
- actual duration and token totals are used to update the budget.

## Phase 3: main experiment

Run 32 samples x 3 conditions x 5 repetitions = 480 calls per backend.
Use 10 repetitions (960 calls/backend) only when the pilot variance or planned
statistical analysis justifies it.

Recommended backend order:

1. local vLLM/Qwen3-8B;
2. OpenRouter/Qwen3-8B with one pinned provider and fallback disabled;
3. direct Gemini 3.5 Flash-Lite.

Recheck model availability, price, and rate-limit tier on the run date. Save the
provider/model identifiers returned by every API response.

## Phase 4: analysis

Report distributions rather than only means. At minimum, group by backend and
compression condition and report:

- captured bytes in each direction;
- packets in each direction;
- TCP payload and protocol overhead;
- completion latency and time to first token;
- request and output token counts;
- bytes per input token and bytes per output token;
- failure/retry rate.

Use paired comparisons because the same sample appears in every compression
condition. Treat backend as an environment factor, not merely another replicate.

## Important confounders

- Provider-side batching, caching, routing, and model revision can change latency.
- Reusing prompts may trigger provider caching even though request bytes are still
  transmitted; record cache-related usage fields when exposed.
- Streaming chunk boundaries are provider-specific and affect packet counts.
- A fixed 30-second window can truncate long generations. Record whether completion
  occurs before the capture ends.
- Output token variation can dominate server-to-client traffic. Keep the output cap
  fixed and use observed token count as a covariate.
