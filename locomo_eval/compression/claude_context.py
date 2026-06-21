from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from .base import BaseCompressor, CompressedResult
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


@dataclass(frozen=True)
class ClaudeContextPolicy:
    """Observable context-management knobs from public Claude-style APIs."""

    recent_turns: int = 4
    retrieval_turns: int = 2
    max_summary_turns: int = 12
    summary_preview_chars: int = 120
    stub_preview_chars: int = 80
    min_recent_turns: int = 1


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
                },
            )

        recent_count = min(max(self.policy.recent_turns, self.policy.min_recent_turns), len(turns))
        best_result: CompressedResult | None = None
        while recent_count >= self.policy.min_recent_turns:
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
                self.policy.min_recent_turns,
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
        recent = list(turns[-recent_count:]) if recent_count > 0 else []
        recent_ids = {turn.dia_id for turn in recent}
        stale = [turn for turn in turns if turn.dia_id not in recent_ids]

        tool_turns = [turn for turn in stale if self._is_tool_like(turn)]
        thinking_turns = [turn for turn in stale if turn not in tool_turns and self._is_thinking_like(turn)]
        compactable = [turn for turn in stale if turn not in tool_turns and turn not in thinking_turns]
        retrieval_limit = 0 if force_minimal_summary else self.policy.retrieval_turns
        retrieved = self._retrieve_relevant(compactable, question, retrieval_limit)
        retrieved_ids = {turn.dia_id for turn in retrieved}

        compaction_sources = [turn for turn in compactable if turn.dia_id not in retrieved_ids]
        max_summary_turns = self.policy.max_summary_turns if summary_turn_limit is None else summary_turn_limit
        summary_turns = compaction_sources[-max_summary_turns:] if max_summary_turns > 0 else []
        if force_minimal_summary:
            summary_turns = summary_turns[-max_summary_turns:] if max_summary_turns > 0 else []

        extra_context = self._build_context_block(
            compaction_sources=summary_turns,
            retrieved_turns=retrieved,
            tool_turns=tool_turns,
            thinking_turns=thinking_turns,
            recent_turns=recent,
            force_minimal_summary=force_minimal_summary,
        )
        kept = self._dedupe_preserve_order([*retrieved, *recent])
        text, token_count = format_and_count_fn(kept, extra_context)
        edits = self._build_edits(tool_turns, thinking_turns, compaction_sources, format_and_count_fn)

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
                "recent_turn_ids": [turn.dia_id for turn in recent],
                "retrieved_turn_ids": [turn.dia_id for turn in retrieved],
                "compaction_source_turn_ids": [turn.dia_id for turn in compaction_sources],
                "compaction_summary_turn_ids": [turn.dia_id for turn in summary_turns],
                "artifact_stub_turn_ids": [turn.dia_id for turn in tool_turns],
                "thinking_cleared_turn_ids": [turn.dia_id for turn in thinking_turns],
                "server_side_items": self._server_side_items(tool_turns, thinking_turns, compaction_sources),
                "applied_edits": [edit.asdict() for edit in edits],
                "compaction_block_id": self._block_id(summary_turns, retrieved, recent),
                "ignored_before_latest_compaction": bool(compaction_sources or tool_turns or thinking_turns),
                "force_minimal_summary": force_minimal_summary,
            },
        )

    def _build_edits(
        self,
        tool_turns: list[Turn],
        thinking_turns: list[Turn],
        compaction_sources: list[Turn],
        format_and_count_fn,
    ) -> list[ContextEdit]:
        edits: list[ContextEdit] = []
        if tool_turns:
            edits.append(ContextEdit("clear_tool_uses_20250919", [turn.dia_id for turn in tool_turns], self._count_turn_tokens(tool_turns, format_and_count_fn)))
        if thinking_turns:
            edits.append(ContextEdit("clear_thinking_20251015", [turn.dia_id for turn in thinking_turns], self._count_turn_tokens(thinking_turns, format_and_count_fn)))
        if compaction_sources:
            edits.append(ContextEdit("compact_20260112", [turn.dia_id for turn in compaction_sources], self._count_turn_tokens(compaction_sources, format_and_count_fn)))
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
        if tool_turns:
            lines.append("Externalized artifact stubs after clear_tool_uses_20250919:")
            lines.extend(self._format_stub(turn) for turn in tool_turns)
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
