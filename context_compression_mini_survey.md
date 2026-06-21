# Server-Side Context Compression for Agentic LLM Systems: A Mini Survey

## Abstract

Long-running LLM agents routinely exceed context-window budgets because they accumulate instructions, tool calls, tool results, intermediate reasoning, file contents, and user feedback. Recent production systems have therefore moved from ad hoc prompt truncation toward server-side context-management layers. This mini survey classifies deployed and research context-compression methods by compression target, execution locus, statefulness, recoverability, and communication impact. We argue that the strongest evidence for reconstructing Claude-Code-like server behavior is not a single leaked or mirrored implementation, but the convergence of public provider APIs, client-agent architecture, and agent-compression research. The public evidence supports a server-side pipeline consisting of threshold-triggered compaction, tool-result and thinking-block clearing, compaction blocks or opaque state items, prompt-cache-aware edit ordering, and client retention of an unmodified canonical transcript. It does not reveal Anthropic's proprietary ranking heuristics, remote feature flags, summarization evaluation, or model-internal cache policies.

## 1. Why this helps reconstruct Claude Code server-side behavior

The attached baseline is useful because it separates context compression into mechanisms that leave different external traces. For Claude Code, this matters more than raw compression ratio. A server-side mechanism can be inferred when API-visible behavior shows that the client sends a full history, the server edits or compacts before inference, and the response carries metadata or blocks describing what was cleared or summarized. Anthropic's public API now makes this distinction explicit: context editing is applied server-side before the prompt reaches Claude while the client maintains the full unmodified conversation history, and server-side compaction creates a `compaction` block that must be passed back on later turns [Anthropic Context Editing](https://platform.claude.com/docs/en/build-with-claude/context-editing), [Anthropic Compaction](https://platform.claude.com/docs/en/build-with-claude/compaction).

For recovery purposes, the survey helps identify which components can be reconstructed cleanly and which cannot. Client-side projection, recent-window retention, tool-output stubbing, structured summaries, and transcript persistence can be implemented from public design principles. Server-side trigger thresholds, ranking policies for which content to clear, prompt-cache invalidation policy, compaction summary prompts, internal telemetry, and private model-service integrations remain uncertain unless explicitly exposed by API documentation or observed through controlled black-box tests.

## 2. Taxonomy

We classify context compression along five axes.

**Target.** The compressed object may be static documents, chat history, tool results, agent trajectories, external memory records, or internal KV-cache state.

**Locus.** Compression may run on the client before upload, in middleware controlled by the application, inside the provider API before model inference, or inside the serving stack after tokenization.

**Statefulness.** Stateless methods compress each prompt independently. Stateful methods produce durable summaries, compaction blocks, memory records, or hidden state that affects later turns.

**Recoverability.** Recoverability depends on whether original content is retained elsewhere. Deletion-only compression has poor recoverability. Projection-based systems retain a full transcript and send only a compact view. Server-side editing can preserve client history while editing the prompt actually seen by the model.

**Communication impact.** Client-side compression reduces uplink bytes. Server-side compaction may reduce later request payloads if the client drops pre-compaction history, but may not reduce the first over-threshold request. KV-cache compression generally reduces server memory or latency rather than network traffic.

## 3. Method families

### 3.1 Client-side prompt and document compression

Client-side prompt compressors reduce tokens before transmission. LongLLMLingua targets long-context and RAG prompts, reorders and removes less relevant content, and reports 1.4x-2.6x end-to-end latency speedups for approximately 10k-token prompts compressed at 2x-6x ratios [LongLLMLingua](https://arxiv.org/abs/2310.06839). LLMLingua-2 reframes prompt compression as extractive token classification trained by data distillation; it reports 3x-6x faster compression and 1.6x-2.9x end-to-end latency speedups at 2x-5x compression ratios [LLMLingua-2](https://arxiv.org/abs/2403.12968).

These methods are strong baselines for communication studies because they directly reduce wire bytes. They are weaker analogs for Claude Code server behavior because they are usually stateless and do not model a persistent agent transcript with tools, files, memory, and recovery semantics.

### 3.2 Server-side context editing

Context editing removes specific low-value blocks before the model sees the prompt. Anthropic documents two server-side editing strategies: `clear_tool_uses_20250919` for old tool interactions and `clear_thinking_20251015` for thinking blocks. The API can preserve recent tool uses, exclude selected tools, clear tool inputs only when configured, and report applied edits and cleared-token statistics [Anthropic Context Editing](https://platform.claude.com/docs/en/build-with-claude/context-editing).

This is one of the most likely components of Claude-Code-like server behavior because coding agents generate large tool outputs, search results, file reads, and command logs. Clearing these blocks is cheaper and safer than summarizing everything: the model has already consumed many old tool results, while the client can still keep the complete transcript.

### 3.3 Server-side semantic compaction

Semantic compaction replaces stale history with a concise summary or state block. Anthropic's `compact_20260112` strategy triggers when input tokens exceed a threshold, generates a conversation summary, returns a `compaction` block, and ignores content blocks before that block on subsequent requests [Anthropic Compaction](https://platform.claude.com/docs/en/build-with-claude/compaction). OpenAI's Responses API exposes server-side compaction through `context_management` with `compact_threshold`; when the rendered token count crosses the threshold, the server emits an encrypted opaque compaction item that carries forward prior state [OpenAI Compaction](https://developers.openai.com/api/docs/guides/compaction).

This family is the closest public analog to server-side Claude Code compaction. The important point is operational rather than linguistic: the server creates a canonical continuation object, the client passes it forward, and earlier content can be ignored or pruned.

### 3.4 Externalization and memory hierarchy

Memory-hierarchy methods reduce active context by moving information out of the prompt and retrieving it later. MemGPT is the canonical research example, using OS-style memory tiers to manage limited context [MemGPT](https://arxiv.org/abs/2310.08560). LangGraph documents production patterns such as trimming messages, deleting messages, summarizing earlier messages, and managing checkpoints [LangGraph Memory](https://docs.langchain.com/oss/python/langgraph/add-memory).

For Claude-Code-like systems, externalization maps naturally to persisted tool outputs, file references, project memory, todo state, and session transcripts. It improves recoverability because the active prompt can be compact while the full evidence remains addressable.

### 3.5 Agent-aware trajectory compression

Agent-aware compressors treat the interaction trajectory as the object of compression. ACON optimizes natural-language compression instructions from failure analysis and reports 26-54% peak-token reduction while preserving or improving task performance across long-horizon agent benchmarks [ACON](https://arxiv.org/html/2510.00615v1). CAT/SWE-Compressor makes context management an explicit tool in long-horizon software-engineering agents, training a model to decide when to compress, how to summarize, and how to reuse compressed representations [Context Management for Long-Horizon SWE-Agents](https://arxiv.org/html/2512.22087v1).

This family is relevant to Claude Code because coding agents are not single-shot RAG systems. The agent must preserve goals, constraints, file edits, failed attempts, test outcomes, and next actions. However, these papers should be used as analogs rather than evidence of Anthropic's internal implementation.

### 3.6 Serving-layer KV or representation compression

KV-cache compression acts below the textual prompt. Methods such as H2O, StreamingLLM, SnapKV, ChunkKV, and TurboQuant reduce inference memory or latency by pruning or compressing attention state. These methods are important for provider infrastructure, but they usually do not change the client-visible transcript, API payload, or compaction block semantics. They should therefore be treated as serving-stack ablations, not direct evidence for Claude Code context management.

## 4. Evidence matrix for reconstructing Claude-Code-like server behavior

| Component | Public evidence | Likelihood in Claude-Code-like server path | Reconstructability |
|---|---|---:|---|
| Threshold-triggered compaction | Anthropic `compact_20260112`; OpenAI `compact_threshold` | Very high | High at API level, low for exact private thresholds |
| Tool-result clearing | Anthropic `clear_tool_uses_20250919` | Very high | High for API semantics |
| Thinking-block clearing | Anthropic `clear_thinking_20251015` | High when extended thinking is used | High for API semantics |
| Recent-turn preservation | Anthropic `pause_after_compaction`; common framework practice | High | High |
| Client keeps canonical transcript while server edits prompt | Anthropic context editing docs | High | High |
| Compaction block passed forward | Anthropic compaction docs | Very high | High |
| Opaque provider state item | OpenAI compaction docs; Anthropic block is human-readable in docs | Medium for Anthropic, high for OpenAI | Medium |
| Prompt-cache-aware context editing | Anthropic compaction/context editing docs mention cache interactions | High | Medium |
| Exact ranking of removable content | Not public beyond configurable strategy parameters | Medium-high | Low |
| Remote feature flags and experiment gates | Not public | High | Low |
| Model-internal KV compression | Research and infrastructure trend | Unknown | Low from API behavior |

## 5. Correct baseline set for experiments

A communication or systems paper should compare methods that produce different traffic and recovery profiles:

| Baseline | Role | Why include |
|---|---|---|
| LongLLMLingua | Client-side long-context compressor | Direct uplink-token reduction; strong historical baseline |
| LLMLingua-2 | Fast faithful extractive compressor | Tests whether faster client compression changes end-to-end latency tradeoffs |
| MemGPT or equivalent memory hierarchy | Externalization baseline | Captures bursty retrieval and recoverable off-prompt storage |
| ACON | Agent-aware learned compression | Tests task retention under long-horizon trajectories |
| CAT-style stateful compaction | Industry-proxy baseline | Closest reproducible analog to agentic server compaction |
| Provider-native compaction | Deployment comparator | Measures real API behavior but is less transparent for ablation |
| KV-cache compression | Server-only ablation | Useful for latency/memory, not wire-byte reduction |

## 6. Recommended experimental protocol

Experiments should report both NLP and systems metrics: input tokens or bytes transmitted per turn, end-to-end latency, time to first token, task success, summary faithfulness, number of compaction events, recovery overhead, and failure rate after forced compaction. Tool-heavy coding traces should be included because they reveal the gap between document compression and agent compression.

Failure analysis should distinguish three cases:

1. Pre-compression decision error: the system triggers too early, too late, or on the wrong content.
2. In-compression information loss: the summary or cleared view drops information needed later.
3. Post-compression access failure: the system preserved information externally but fails to retrieve it when needed.

For Claude-Code-like reconstruction, black-box tests should vary tool-output size, number of old tool calls, thinking-block volume, cache breakpoints, and compaction thresholds. The goal is not to recover private source, but to infer the observable state machine: when edits occur, what response metadata is emitted, what content must be passed back, and whether the client or server owns the canonical compacted view.

## 7. Assessment of the original baseline

The original draft is directionally correct and useful, especially in separating client-side compression, stateful compaction, external memory, agent-aware compression, and KV compression. Its main weaknesses are presentation and evidence hygiene: citations are broken, several claims about newly published systems should be softened, citation counts should be removed or moved to an appendix, and provider-native compaction should be treated as an evidence source for API semantics rather than as proof of private Claude Code internals. A research-paper-standard version should clearly separate verified public API behavior, third-party implementation analogs, and speculative reconstruction hypotheses.

## 8. Conclusion

The strongest recoverable picture of Claude-Code-like server-side context management is a layered system: clear old tool results and thinking blocks, preserve recent interactions, compact stale conversation into a continuation block, keep the client transcript canonical where possible, and use external memory for artifacts that must remain recoverable. The exact internal ranking heuristics, feature flags, and service-level implementation remain unrecoverable from public evidence alone. Therefore, a defensible mini survey should frame Claude Code recovery as an inference problem over public API behavior and convergent industry architecture, not as a claim of exact implementation equivalence.

