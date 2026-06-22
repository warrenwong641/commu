# Claude-Code-Like Context Management Reconstruction

This repo implements a clean-room experimental baseline for Claude-Code-like context management as `claude_context`.
It is a behavioral reconstruction for evaluation, not a claim about Anthropic private source.

## Public References

| Reference | What it contributes |
|---|---|
| [Anthropic Context Editing](https://platform.claude.com/docs/en/build-with-claude/context-editing) | Server-side `clear_tool_uses_20250919` and `clear_thinking_20251015` semantics; client history can remain unmodified while the server edits model-visible context. |
| [Anthropic Compaction](https://platform.claude.com/docs/en/build-with-claude/compaction) | Server-side `compact_20260112`, threshold-triggered compaction, compaction blocks, and pass-forward behavior. |
| [win4r/cc-notebook](https://github.com/win4r/cc-notebook) | High-level Claude Code context-management notes, including local compaction layers and server/cache editing clues. |
| [claude-code-best/claude-code](https://github.com/claude-code-best/claude-code) | Third-party architecture reference for modules such as compact, context collapse, tool result storage, token budgets, and query transitions. |
| [openai/codex compaction](https://github.com/openai/codex/blob/main/codex-rs/core/src/compact.rs) | Public comparable implementation for local compaction orchestration, history rebuilding, and remote compaction integration. |

## Implemented Baseline

The compressor lives in [`locomo_eval/experimental/claude_context.py`](locomo_eval/experimental/claude_context.py).
It projects a full immutable transcript into an API-visible context:

```text
canonical transcript
        |
        v
claude_context projection
        |
        +-- recent raw turns
        +-- retrieved high-salience stale turns
        +-- compaction block for older state
        +-- tool-output artifact stubs
        +-- cleared thinking markers
        |
        v
model-visible context
```

## Server-Side Items Recovered

| Item | Implemented as | Confidence | Notes |
|---|---|---:|---|
| Tool-result clearing | `clear_tool_uses_20250919` metadata and artifact stubs | High | Publicly documented API primitive; local classifier is heuristic. |
| Thinking clearing | `clear_thinking_20251015` metadata | High | Publicly documented API primitive; relevant when thinking blocks are present. |
| Semantic compaction | `compact_20260112` metadata plus `[CLAUDE_CONTEXT_BLOCK]` | High | Publicly documented API primitive; summary text is heuristic and reproducible. |
| Recent-turn preservation | `recent_turn_ids` and verbatim kept turns | High | Common server/client policy; directly useful for active task continuity. |
| Carry-forward state | `compaction_block_id` and `extra_context` | Medium-high | Models pass-forward behavior without claiming private encoding. |
| Dual-view transcript | `canonical_transcript_turns` vs `api_visible_turns` | High | Matches documented context-editing split between client history and server-visible context. |
| Cache-aware editing | `stable_prefix_hash`, `cache_boundary_index`, and invalidation metadata | Medium | Models stable-prefix preservation; does not claim provider cache internals. |
| Exact clearing ranker | Auditable clear candidate scores | Medium-low | Scores cost, age, noise, relevance, and protection reasons; private production ranking remains unrecovered. |
| Remote config/feature flags | `ClaudeContextPolicy` and YAML knobs | Low | Represented as configurable policy, not as production constants. |

## Experiment Usage

Use `configs/claude_context_experiment.yaml` for the CC-like proxy. The default LoCoMo config stays provider-neutral and does not include this experimental baseline.

Config knobs live under `method_options.claude_context`:

```yaml
method_options:
  claude_context:
    recent_turns: 4
    retrieval_turns: 2
    max_summary_turns: 12
    summary_preview_chars: 120
    stub_preview_chars: 80
    enable_tool_clearing: true
    enable_thinking_clearing: true
    enable_compaction: true
    enable_artifact_stubs: true
    enable_cache_awareness: true
    tool_clear_threshold_tokens: null
    thinking_clear_threshold_tokens: null
    compaction_threshold_tokens: null
    cache_prefix_turns: 0
    allow_cache_invalidation_on_emergency: true
```

Primary metadata fields for analysis:

```text
server_side_items
applied_edits
recent_turn_ids
retrieved_turn_ids
compaction_source_turn_ids
compaction_summary_turn_ids
artifact_stub_turn_ids
thinking_cleared_turn_ids
canonical_transcript_turns
api_visible_turns
stable_prefix_turn_ids
stable_prefix_hash
cache_boundary_index
edits_before_cache_boundary
cache_invalidated_by_edits
cache_invalidated_by_emergency
clearing_rankings
clearing_policy
protected_turn_ids
clear_reasons
ignored_before_latest_compaction
```

## Known Limits

This baseline does not recover Anthropic private thresholds, rankers, hidden prompts, prompt-cache invalidation rules, remote feature flags, telemetry, or model-serving KV-cache policies. Those should be studied with black-box API experiments and represented as policy parameters rather than hard-coded implementation claims.
