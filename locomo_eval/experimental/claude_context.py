from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from locomo_eval.compression.base import BaseCompressor, CompressedResult
from locomo_eval.locomo.schemas import Turn


_WORD_RE = re.compile(r"[a-zA-Z0-9_]+")
_TOOL_HINTS = (
    "[tool_result]",
    "<tool_result",
    "tool result",
    "bash output",
    "command output",
    "grep output",
    "search results",
    "traceback",
    "stack trace",
    "stdout",
    "stderr",
)
_THINKING_HINTS = ("[thinking]", "<thinking", "thinking:", "reasoning:")
_CONSTRAINT_HINTS = ("must", "always", "never", "do not", "don't", "remember", "important", "requirement", "constraint")


@dataclass(frozen=True)
class ClaudeContextPolicy:
    """Observable context-management knobs from public Claude-style APIs."""

    recent_turns: int = 4
    retrieval_turns: int = 2
    max_summary_turns: int = 12
    summary_preview_chars: int = 120
    stub_preview_chars: int = 80
    min_recent_turns: int = 1
    enable_tool_clearing: bool = True
    enable_thinking_clearing: bool = True
    enable_compaction: bool = True
    enable_artifact_stubs: bool = True
    enable_cache_awareness: bool = True
    tool_clear_threshold_tokens: int | None = None
    thinking_clear_threshold_tokens: int | None = None
    compaction_threshold_tokens: int | None = None
    cache_prefix_turns: int = 0
    allow_cache_invalidation_on_emergency: bool = True


@dataclass(frozen=True)
class ContextEdit:
    edit_type: str
    turn_ids: list[str]
    cleared_input_tokens: int

    def asdict(self) -> dict[str, Any]:
        return {
            "type": self.edit_type,
            "turn_ids": self.turn_ids,
            "cleared_input_tokens": self.cleared_input_tokens,
        }


@dataclass(frozen=True)
class ClearCandidate:
    turn: Turn
    kind: str
    token_cost: int
    age_score: float
    relevance_score: float
    noise_score: float
    protected_reasons: list[str]
    clear_score: float

    def asdict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn.dia_id,
            "kind": self.kind,
            "token_cost": self.token_cost,
            "age_score": round(self.age_score, 4),
            "relevance_score": round(self.relevance_score, 4),
            "noise_score": round(self.noise_score, 4),
            "protected_reasons": self.protected_reasons,
            "clear_score": round(self.clear_score, 4),
        }


class ClaudeContextCompressor(BaseCompressor):
    """Clean-room Claude-Code-like server context-management baseline.

    The compressor models public, recoverable server-side primitives rather than
    Anthropic private internals:

    * clear old tool-use/tool-result-like turns;
    * clear old thinking-like turns;
    * preserve a verbatim recent window;
    * summarize stale turns into a carry-forward compaction block;
    * externalize bulky artifacts as stable stubs.

    It intentionally keeps the full input transcript immutable from the caller's
    point of view and only returns the projected API-visible context.
    """

    name = "claude_context"

    def __init__(self, policy: ClaudeContextPolicy | None = None) -> None:
        self.policy = policy or ClaudeContextPolicy()

    def compress(self, turns: list[Turn], question: str, budget: int | None, format_and_count_fn) -> CompressedResult:
        if not turns:
            extra_context = self._build_context_block([], [], [], [], [], force_minimal_summary=False)
            text, token_count = format_and_count_fn([], extra_context)
            budget_satisfied = budget is None or token_count <= budget
            return CompressedResult(
                kept_turns=[],
                kept_turn_ids=[],
                compressed_context=text,
                token_count=token_count,
                extra_context=extra_context,
                metadata={
                    "budget": budget,
                    "mode": "empty_transcript",
                    "server_side_items": [],
                    "canonical_transcript_turns": 0,
                    "budget_satisfied": budget_satisfied,
                    "budget_error": (
                        None
                        if budget_satisfied
                        else self._budget_error(token_count, budget)
                    ),
                },
            )

        if budget is None:
            text, token_count = format_and_count_fn(list(turns), None)
            return CompressedResult(
                kept_turns=list(turns),
                kept_turn_ids=[turn.dia_id for turn in turns],
                compressed_context=text,
                token_count=token_count,
                metadata={
                    "budget": None,
                    "mode": "full_transcript",
                    "server_side_items": [],
                    "canonical_transcript_turns": len(turns),
                    "budget_satisfied": True,
                    "budget_error": None,
                },
            )

        minimum_recent = min(max(0, self.policy.min_recent_turns), len(turns))
        recent_count = min(
            max(0, self.policy.recent_turns, minimum_recent),
            len(turns),
        )
        best_result: CompressedResult | None = None
        while recent_count >= minimum_recent:
            result = self._project(turns, question, budget, recent_count, format_and_count_fn)
            best_result = result
            if result.token_count <= budget:
                return result
            recent_count -= 1

        assert best_result is not None
        for summary_turns in (3, 1, 0):
            compact_result = self._project(
                turns,
                question,
                budget,
                minimum_recent,
                format_and_count_fn,
                force_minimal_summary=True,
                summary_turn_limit=summary_turns,
            )
            if compact_result.token_count <= budget:
                return compact_result
            if compact_result.token_count <= best_result.token_count:
                best_result = compact_result
        return best_result

    def _project(
        self,
        turns: list[Turn],
        question: str,
        budget: int,
        recent_count: int,
        format_and_count_fn,
        force_minimal_summary: bool = False,
        summary_turn_limit: int | None = None,
    ) -> CompressedResult:
        original_tokens = self._count_turn_tokens(turns, format_and_count_fn)
        recent = list(turns[-recent_count:]) if recent_count > 0 else []
        recent_ids = {turn.dia_id for turn in recent}
        stable_prefix = self._stable_prefix_turns(turns, recent_ids)
        stable_prefix_ids = {turn.dia_id for turn in stable_prefix}
        protected_ids = set(recent_ids)
        if self.policy.enable_cache_awareness and (
            not force_minimal_summary or not self.policy.allow_cache_invalidation_on_emergency
        ):
            protected_ids.update(stable_prefix_ids)
        stale = [turn for turn in turns if turn.dia_id not in recent_ids]

        clear_candidates = self._rank_clear_candidates(stale, question, original_tokens, protected_ids, turns, format_and_count_fn)
        protected_candidate_ids = {candidate.turn.dia_id for candidate in clear_candidates if candidate.protected_reasons}
        effective_protected_ids = protected_ids | protected_candidate_ids
        tool_turns = [
            candidate.turn
            for candidate in clear_candidates
            if candidate.kind == "tool"
            and not candidate.protected_reasons
            and self.policy.enable_tool_clearing
            and self._threshold_met(original_tokens, self.policy.tool_clear_threshold_tokens)
        ]
        thinking_turns = [
            candidate.turn
            for candidate in clear_candidates
            if candidate.kind == "thinking"
            and not candidate.protected_reasons
            and self.policy.enable_thinking_clearing
            and self._threshold_met(original_tokens, self.policy.thinking_clear_threshold_tokens)
        ]
        chronological_key = lambda turn: self._turn_sort_key(turns)(turn.dia_id)
        tool_turns = sorted(tool_turns, key=chronological_key)
        thinking_turns = sorted(thinking_turns, key=chronological_key)
        compactable = [turn for turn in stale if turn not in tool_turns and turn not in thinking_turns]
        retrieval_limit = 0 if force_minimal_summary else self.policy.retrieval_turns
        retrieved = self._retrieve_relevant(compactable, question, retrieval_limit)
        retrieved_ids = {turn.dia_id for turn in retrieved}

        should_compact = self.policy.enable_compaction and self._threshold_met(original_tokens, self.policy.compaction_threshold_tokens)
        compaction_candidates = [
            turn
            for turn in compactable
            if turn.dia_id not in retrieved_ids and turn.dia_id not in effective_protected_ids
        ] if should_compact else []
        max_summary_turns = self.policy.max_summary_turns if summary_turn_limit is None else summary_turn_limit
        compaction_sources = compaction_candidates[-max_summary_turns:] if max_summary_turns > 0 else []
        compaction_source_ids = {turn.dia_id for turn in compaction_sources}
        budget_evicted_turns = (
            [
                turn
                for turn in compactable
                if turn.dia_id not in compaction_source_ids
                and turn.dia_id not in retrieved_ids
                and turn.dia_id not in effective_protected_ids
            ]
            if force_minimal_summary
            else []
        )
        budget_evicted_ids = {turn.dia_id for turn in budget_evicted_turns}

        extra_context = self._build_context_block(
            compaction_sources=compaction_sources,
            retrieved_turns=retrieved,
            tool_turns=tool_turns,
            thinking_turns=thinking_turns,
            recent_turns=recent,
            force_minimal_summary=force_minimal_summary,
        )
        retained_stale = [
            turn
            for turn in compactable
            if turn.dia_id not in compaction_source_ids
            and turn.dia_id not in budget_evicted_ids
            and turn.dia_id not in retrieved_ids
        ]
        kept = self._dedupe_preserve_order([*retained_stale, *retrieved, *recent])
        text, token_count = format_and_count_fn(kept, extra_context)

        protected_budget_evicted_turns: list[Turn] = []
        if force_minimal_summary and token_count > budget:
            kept_candidate_ids = {turn.dia_id for turn in kept}
            protected_candidates = [
                candidate.turn
                for candidate in clear_candidates
                if candidate.protected_reasons
                and candidate.turn.dia_id in kept_candidate_ids
                and candidate.turn.dia_id not in recent_ids
                and (
                    self.policy.allow_cache_invalidation_on_emergency
                    or candidate.turn.dia_id not in stable_prefix_ids
                )
            ]
            for turn in protected_candidates:
                protected_budget_evicted_turns.append(turn)
                kept = [
                    kept_turn
                    for kept_turn in kept
                    if kept_turn.dia_id != turn.dia_id
                ]
                text, token_count = format_and_count_fn(kept, extra_context)
                if token_count <= budget:
                    break

        budget_evicted_turns = sorted(
            [*budget_evicted_turns, *protected_budget_evicted_turns],
            key=chronological_key,
        )
        kept_ids = {turn.dia_id for turn in kept}
        cache_invalidated_by_emergency = bool(
            force_minimal_summary
            and self.policy.allow_cache_invalidation_on_emergency
            and (stable_prefix_ids - kept_ids)
        )
        budget_satisfied = token_count <= budget
        edits = self._build_edits(
            tool_turns,
            thinking_turns,
            compaction_sources,
            budget_evicted_turns,
            format_and_count_fn,
        )
        cache_metadata = self._cache_metadata(
            turns=turns,
            stable_prefix=stable_prefix,
            edited_turn_ids={
                turn.dia_id
                for turn in [*tool_turns, *thinking_turns, *compaction_sources, *budget_evicted_turns]
            },
            cache_invalidated_by_emergency=cache_invalidated_by_emergency,
        )
        protected_turn_ids = sorted(
            {candidate.turn.dia_id for candidate in clear_candidates if candidate.protected_reasons},
            key=self._turn_sort_key(turns),
        )

        return CompressedResult(
            kept_turns=kept,
            kept_turn_ids=[turn.dia_id for turn in kept],
            compressed_context=text,
            token_count=token_count,
            extra_context=extra_context,
            metadata={
                "budget": budget,
                "mode": "claude_code_like_projection",
                "canonical_transcript_turns": len(turns),
                "api_visible_turns": len(kept),
                "original_tokens": original_tokens,
                "recent_turn_ids": [turn.dia_id for turn in recent],
                "stable_prefix_turn_ids": [turn.dia_id for turn in stable_prefix],
                "retrieved_turn_ids": [turn.dia_id for turn in retrieved],
                "compaction_source_turn_ids": [turn.dia_id for turn in compaction_sources],
                "compaction_summary_turn_ids": [turn.dia_id for turn in compaction_sources],
                "budget_evicted_turn_ids": [turn.dia_id for turn in budget_evicted_turns],
                "protected_budget_evicted_turn_ids": [
                    turn.dia_id for turn in protected_budget_evicted_turns
                ],
                "artifact_stub_turn_ids": [turn.dia_id for turn in tool_turns],
                "thinking_cleared_turn_ids": [turn.dia_id for turn in thinking_turns],
                "server_side_items": self._server_side_items(tool_turns, thinking_turns, compaction_sources),
                "applied_edits": [edit.asdict() for edit in edits],
                "clearing_rankings": [candidate.asdict() for candidate in clear_candidates],
                "clearing_policy": self._policy_metadata(),
                "protected_turn_ids": protected_turn_ids,
                "clear_reasons": {
                    **self._clear_reasons(clear_candidates, tool_turns, thinking_turns),
                    **{
                        turn.dia_id: "evicted by force-minimal projection to satisfy the token budget"
                        for turn in budget_evicted_turns
                    },
                },
                "compaction_block_id": self._block_id(compaction_sources, retrieved, recent),
                "ignored_before_latest_compaction": bool(
                    compaction_sources or tool_turns or thinking_turns or budget_evicted_turns
                ),
                "force_minimal_summary": force_minimal_summary,
                "budget_satisfied": budget_satisfied,
                "budget_error": (
                    None
                    if budget_satisfied
                    else self._budget_error(token_count, budget)
                ),
                **cache_metadata,
            },
        )

    def _build_edits(
        self,
        tool_turns: list[Turn],
        thinking_turns: list[Turn],
        compaction_sources: list[Turn],
        budget_evicted_turns: list[Turn],
        format_and_count_fn,
    ) -> list[ContextEdit]:
        edits: list[ContextEdit] = []
        if tool_turns:
            edits.append(ContextEdit("clear_tool_uses_20250919", [turn.dia_id for turn in tool_turns], self._count_turn_tokens(tool_turns, format_and_count_fn)))
        if thinking_turns:
            edits.append(ContextEdit("clear_thinking_20251015", [turn.dia_id for turn in thinking_turns], self._count_turn_tokens(thinking_turns, format_and_count_fn)))
        if compaction_sources:
            edits.append(ContextEdit("compact_20260112", [turn.dia_id for turn in compaction_sources], self._count_turn_tokens(compaction_sources, format_and_count_fn)))
        if budget_evicted_turns:
            edits.append(
                ContextEdit(
                    "evict_for_token_budget",
                    [turn.dia_id for turn in budget_evicted_turns],
                    self._count_turn_tokens(budget_evicted_turns, format_and_count_fn),
                )
            )
        return edits

    def _server_side_items(
        self,
        tool_turns: list[Turn],
        thinking_turns: list[Turn],
        compaction_sources: list[Turn],
    ) -> list[str]:
        items: list[str] = []
        if tool_turns:
            items.append("clear_tool_uses_20250919")
        if thinking_turns:
            items.append("clear_thinking_20251015")
        if compaction_sources:
            items.append("compact_20260112")
        return items

    def _build_context_block(
        self,
        compaction_sources: list[Turn],
        retrieved_turns: list[Turn],
        tool_turns: list[Turn],
        thinking_turns: list[Turn],
        recent_turns: list[Turn],
        force_minimal_summary: bool,
    ) -> str:
        lines = [
            "[CLAUDE_CONTEXT_BLOCK type=compaction_20260112]",
            "Purpose: server-side context projection for a long-running coding/chat agent.",
            "Policy: keep recent raw turns, carry forward stale state, and keep the client transcript canonical.",
        ]
        if recent_turns:
            lines.append("Recent raw turn ids preserved: " + ", ".join(turn.dia_id for turn in recent_turns))
        if retrieved_turns:
            lines.append("Retrieved high-salience turns:")
            lines.extend(self._format_turn(turn, "memory") for turn in retrieved_turns)
        if compaction_sources:
            lines.append("Compacted prior state:")
            lines.extend(self._format_turn(turn, "summary", minimal=force_minimal_summary) for turn in compaction_sources)
        if tool_turns and self.policy.enable_artifact_stubs:
            lines.append("Externalized artifact stubs after clear_tool_uses_20250919:")
            lines.extend(self._format_stub(turn) for turn in tool_turns)
        elif tool_turns:
            lines.append("Cleared tool blocks after clear_tool_uses_20250919:")
            lines.extend(f"- turn={turn.dia_id} speaker={turn.speaker}" for turn in tool_turns)
        if thinking_turns:
            lines.append("Cleared thinking blocks after clear_thinking_20251015:")
            lines.extend(f"- turn={turn.dia_id} speaker={turn.speaker}" for turn in thinking_turns)
        lines.append("[/CLAUDE_CONTEXT_BLOCK]")
        return "\n".join(lines)

    def _format_turn(self, turn: Turn, label: str, minimal: bool = False) -> str:
        preview_limit = 64 if minimal else self.policy.summary_preview_chars
        fields = [f"turn={turn.dia_id}", f"speaker={turn.speaker}"]
        if turn.session_id:
            fields.append(f"session={turn.session_id}")
        if turn.timestamp:
            fields.append(f"date={turn.timestamp}")
        return f"- {label}({' '.join(fields)}): {self._preview(turn.text, preview_limit)}"

    def _format_stub(self, turn: Turn) -> str:
        digest = hashlib.sha1(f"{turn.dia_id}:{turn.text}".encode("utf-8")).hexdigest()[:12]
        return (
            f"- artifact_stub id=artifact:{turn.dia_id}:{digest} turn={turn.dia_id} "
            f"speaker={turn.speaker} preview={self._preview(turn.text, self.policy.stub_preview_chars)!r}"
        )

    def _retrieve_relevant(self, turns: list[Turn], question: str, limit: int) -> list[Turn]:
        if limit <= 0:
            return []
        query_terms = self._terms(question)
        if not query_terms:
            return []
        scored: list[tuple[int, int, Turn]] = []
        for index, turn in enumerate(turns):
            overlap = len(query_terms & self._terms(turn.text))
            if overlap:
                scored.append((overlap, index, turn))
        scored.sort(key=lambda item: (-item[0], item[1]))
        chosen = [turn for _, _, turn in scored[:limit]]
        index_by_id = {turn.dia_id: index for index, turn in enumerate(turns)}
        return sorted(chosen, key=lambda turn: index_by_id[turn.dia_id])

    def _rank_clear_candidates(
        self,
        stale_turns: list[Turn],
        question: str,
        original_tokens: int,
        protected_ids: set[str],
        all_turns: list[Turn],
        format_and_count_fn,
    ) -> list[ClearCandidate]:
        del original_tokens
        query_terms = self._terms(question)
        max_index = max(1, len(all_turns) - 1)
        index_by_id = {turn.dia_id: index for index, turn in enumerate(all_turns)}
        candidates: list[ClearCandidate] = []
        for turn in stale_turns:
            kind = self._candidate_kind(turn)
            token_cost = self._count_turn_tokens([turn], format_and_count_fn)
            turn_terms = self._terms(turn.text)
            overlap = len(query_terms & turn_terms)
            relevance_score = overlap / max(1, len(query_terms))
            age_score = 1.0 - (index_by_id.get(turn.dia_id, max_index) / max_index)
            noise_score = self._noise_score(turn, kind)
            protected_reasons = self._protected_reasons(turn, relevance_score, protected_ids)
            clear_score = (token_cost / 100.0) + age_score + noise_score - (2.5 * relevance_score)
            if protected_reasons:
                clear_score -= 100.0
            candidates.append(
                ClearCandidate(
                    turn=turn,
                    kind=kind,
                    token_cost=token_cost,
                    age_score=age_score,
                    relevance_score=relevance_score,
                    noise_score=noise_score,
                    protected_reasons=protected_reasons,
                    clear_score=clear_score,
                )
            )
        candidates.sort(key=lambda candidate: (-candidate.clear_score, index_by_id.get(candidate.turn.dia_id, 0)))
        return candidates

    def _candidate_kind(self, turn: Turn) -> str:
        if self._is_tool_like(turn):
            return "tool"
        if self._is_thinking_like(turn):
            return "thinking"
        return "normal"

    def _noise_score(self, turn: Turn, kind: str) -> float:
        kind_score = {"tool": 1.0, "thinking": 0.85, "normal": 0.0}[kind]
        length_score = min(len(turn.text) / 1_200.0, 1.0)
        repeat_score = 0.3 if self._looks_repetitive(turn.text) else 0.0
        return kind_score + length_score + repeat_score

    def _protected_reasons(self, turn: Turn, relevance_score: float, protected_ids: set[str]) -> list[str]:
        reasons: list[str] = []
        if turn.dia_id in protected_ids:
            reasons.append("cache_or_recent_boundary")
        if relevance_score >= 0.30:
            reasons.append("question_relevant")
        lower = turn.text.lower()
        if any(hint in lower for hint in _CONSTRAINT_HINTS):
            reasons.append("user_constraint")
        return reasons

    def _stable_prefix_turns(self, turns: list[Turn], recent_ids: set[str]) -> list[Turn]:
        if not self.policy.enable_cache_awareness or self.policy.cache_prefix_turns <= 0:
            return []
        stable: list[Turn] = []
        for turn in turns:
            if turn.dia_id in recent_ids:
                continue
            stable.append(turn)
            if len(stable) >= self.policy.cache_prefix_turns:
                break
        return stable

    def _cache_metadata(
        self,
        turns: list[Turn],
        stable_prefix: list[Turn],
        edited_turn_ids: set[str],
        cache_invalidated_by_emergency: bool,
    ) -> dict[str, Any]:
        stable_ids = [turn.dia_id for turn in stable_prefix]
        stable_id_set = set(stable_ids)
        edits_before_boundary = sorted(stable_id_set & edited_turn_ids, key=self._turn_sort_key(turns))
        return {
            "cache_awareness_enabled": self.policy.enable_cache_awareness,
            "cache_boundary_index": len(stable_prefix),
            "stable_prefix_turn_ids": stable_ids,
            "stable_prefix_hash": self._prefix_hash(stable_prefix),
            "edits_before_cache_boundary": edits_before_boundary,
            "cache_invalidated_by_edits": bool(edits_before_boundary),
            "cache_invalidated_by_emergency": cache_invalidated_by_emergency,
        }

    def _policy_metadata(self) -> dict[str, Any]:
        return {
            "enable_tool_clearing": self.policy.enable_tool_clearing,
            "enable_thinking_clearing": self.policy.enable_thinking_clearing,
            "enable_compaction": self.policy.enable_compaction,
            "enable_artifact_stubs": self.policy.enable_artifact_stubs,
            "enable_cache_awareness": self.policy.enable_cache_awareness,
            "tool_clear_threshold_tokens": self.policy.tool_clear_threshold_tokens,
            "thinking_clear_threshold_tokens": self.policy.thinking_clear_threshold_tokens,
            "compaction_threshold_tokens": self.policy.compaction_threshold_tokens,
            "cache_prefix_turns": self.policy.cache_prefix_turns,
            "allow_cache_invalidation_on_emergency": self.policy.allow_cache_invalidation_on_emergency,
        }

    def _clear_reasons(
        self,
        candidates: list[ClearCandidate],
        tool_turns: list[Turn],
        thinking_turns: list[Turn],
    ) -> dict[str, str]:
        cleared_ids = {turn.dia_id for turn in [*tool_turns, *thinking_turns]}
        reasons: dict[str, str] = {}
        for candidate in candidates:
            if candidate.turn.dia_id in cleared_ids:
                reasons[candidate.turn.dia_id] = (
                    f"{candidate.kind} block cleared; score={candidate.clear_score:.4f}; "
                    f"tokens={candidate.token_cost}; noise={candidate.noise_score:.4f}; relevance={candidate.relevance_score:.4f}"
                )
        return reasons

    def _threshold_met(self, original_tokens: int, threshold: int | None) -> bool:
        return threshold is None or original_tokens >= threshold

    def _prefix_hash(self, turns: list[Turn]) -> str | None:
        if not turns:
            return None
        payload = "\n".join(f"{turn.dia_id}\0{turn.speaker}\0{turn.text}" for turn in turns)
        return "cache_" + hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]

    def _turn_sort_key(self, turns: list[Turn]):
        index_by_id = {turn.dia_id: index for index, turn in enumerate(turns)}
        return lambda turn_id: index_by_id.get(turn_id, len(turns))

    def _looks_repetitive(self, text: str) -> bool:
        terms = list(_WORD_RE.finditer(text.lower()))
        if len(terms) < 16:
            return False
        words = [term.group(0) for term in terms]
        return len(set(words)) / len(words) < 0.45

    def _is_tool_like(self, turn: Turn) -> bool:
        text = turn.text.lower()
        return any(hint in text for hint in _TOOL_HINTS) or len(turn.text) > 1_200

    def _is_thinking_like(self, turn: Turn) -> bool:
        text = turn.text.lower()
        return any(hint in text for hint in _THINKING_HINTS)

    def _terms(self, text: str) -> set[str]:
        return {match.group(0).lower() for match in _WORD_RE.finditer(text) if len(match.group(0)) > 2}

    def _preview(self, text: str, limit: int) -> str:
        normalized = " ".join(text.split())
        if len(normalized) <= limit:
            return normalized
        return normalized[: max(0, limit - 3)].rstrip() + "..."

    def _count_turn_tokens(self, turns: list[Turn], format_and_count_fn) -> int:
        if not turns:
            return 0
        _, count = format_and_count_fn(turns, None)
        return count

    def _budget_error(self, token_count: int, budget: int) -> str:
        return (
            "minimum non-evictable context and projection overhead exceed the token "
            f"budget ({token_count} > {budget})"
        )

    def _block_id(self, *groups: list[Turn]) -> str:
        payload = "|".join(turn.dia_id for group in groups for turn in group)
        if not payload:
            payload = "empty"
        return "ccctx_" + hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]

    def _dedupe_preserve_order(self, turns: list[Turn]) -> list[Turn]:
        seen: set[str] = set()
        kept: list[Turn] = []
        for turn in turns:
            if turn.dia_id in seen:
                continue
            seen.add(turn.dia_id)
            kept.append(turn)
        return kept


def build_claude_context_compressor(options: dict[str, Any] | None = None) -> ClaudeContextCompressor:
    """Factory used by the generic experiment runner extension hook."""

    return ClaudeContextCompressor(ClaudeContextPolicy(**(options or {})))
