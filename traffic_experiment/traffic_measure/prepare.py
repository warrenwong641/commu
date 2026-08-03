from __future__ import annotations

import random
import time
from pathlib import Path
from typing import Any, Iterable

from locomo_eval.locomo.formatter import format_turn_as_evidence
from locomo_eval.locomo.loader import load_conversations
from locomo_eval.locomo.schemas import Conversation, QAExample

from .common import sha256_json, utc_now, write_jsonl

DEFAULT_SYSTEM_PROMPT = (
    "Answer the question using only the supplied conversation history. "
    "Give one short phrase unless a full sentence is necessary."
)

SUPPORTED_CONDITIONS = {
    "no_compression": 1.0,
    "longllmlingua_2x": 0.5,
    "longllmlingua_4x": 0.25,
}


def _eligible_examples(conversations: Iterable[Conversation]) -> list[tuple[Conversation, QAExample]]:
    eligible: list[tuple[Conversation, QAExample]] = []
    for conversation in conversations:
        for qa in conversation.qa_examples:
            if qa.category in {1, 2, 3, 4}:
                eligible.append((conversation, qa))
    return eligible


def select_examples(
    conversations: list[Conversation],
    count: int,
    seed: int,
) -> list[tuple[Conversation, QAExample]]:
    eligible = _eligible_examples(conversations)
    if count <= 0:
        raise ValueError("sample count must be positive")
    if len(eligible) < count:
        raise ValueError(f"requested {count} samples, but only {len(eligible)} eligible QA examples exist")
    selected = random.Random(seed).sample(eligible, count)
    return sorted(selected, key=lambda item: (item[0].conversation_id, item[1].question_id))


def _context_blocks(conversation: Conversation) -> list[str]:
    return [format_turn_as_evidence(turn) for turn in conversation.all_turns]


def _build_messages(system_prompt: str, context: str, question: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": f"Conversation history:\n\n{context}\n\nQuestion:\n{question}",
        },
    ]


def _load_compressor(model_name: str, device_map: str):
    try:
        from llmlingua import PromptCompressor
    except ImportError as exc:
        raise RuntimeError(
            "LongLLMLingua is required for compressed conditions. "
            "Install requirements-compression.txt in the preparation environment."
        ) from exc
    return PromptCompressor(model_name=model_name, device_map=device_map)


def prepare_manifest(
    data_dir: Path,
    output_path: Path,
    sample_count: int,
    seed: int,
    conditions: list[str],
    compressor_model: str,
    compressor_device: str,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
) -> list[dict[str, Any]]:
    unknown = sorted(set(conditions) - set(SUPPORTED_CONDITIONS))
    if unknown:
        raise ValueError(f"unsupported condition(s): {', '.join(unknown)}")

    conversations = load_conversations(data_dir)
    selected = select_examples(conversations, sample_count, seed)
    requires_compressor = any(name != "no_compression" for name in conditions)
    compressor = _load_compressor(compressor_model, compressor_device) if requires_compressor else None

    rows: list[dict[str, Any]] = []
    prepared_at = utc_now()
    for conversation, qa in selected:
        blocks = _context_blocks(conversation)
        uncompressed_context = "\n\n".join(blocks)
        sample_id = f"{conversation.conversation_id}::{qa.question_id}"

        for condition in conditions:
            rate = SUPPORTED_CONDITIONS[condition]
            compression_started = time.perf_counter()
            compression_metadata: dict[str, Any]
            if condition == "no_compression":
                context = uncompressed_context
                compression_metadata = {
                    "method": "none",
                    "target_rate": 1.0,
                    "origin_tokens": None,
                    "compressed_tokens": None,
                    "reported_ratio": "1.0x",
                }
            else:
                assert compressor is not None
                result = compressor.compress_prompt(
                    blocks,
                    question=qa.question,
                    rate=rate,
                    condition_in_question="after_condition",
                    reorder_context="sort",
                    dynamic_context_compression_ratio=0.3,
                    condition_compare=True,
                    context_budget="+100",
                    rank_method="longllmlingua",
                    concate_question=False,
                )
                context = str(result["compressed_prompt"])
                compression_metadata = {
                    "method": "LongLLMLingua",
                    "compressor_model": compressor_model,
                    "target_rate": rate,
                    "origin_tokens": result.get("origin_tokens"),
                    "compressed_tokens": result.get("compressed_tokens"),
                    "reported_ratio": result.get("ratio"),
                    "reported_rate": result.get("rate"),
                }

            messages = _build_messages(system_prompt, context, qa.question)
            compression_metadata["elapsed_seconds"] = round(time.perf_counter() - compression_started, 6)
            row = {
                "manifest_version": 1,
                "request_id": f"{sample_id}::{condition}",
                "sample_id": sample_id,
                "conversation_id": conversation.conversation_id,
                "question_id": qa.question_id,
                "condition": condition,
                "question": qa.question,
                "reference_answer": qa.answer,
                "category": qa.category,
                "evidence_ids": qa.evidence_ids,
                "messages": messages,
                "messages_sha256": sha256_json(messages),
                "compression": compression_metadata,
                "prepared_at_utc": prepared_at,
                "selection_seed": seed,
            }
            rows.append(row)

    write_jsonl(output_path, rows)
    return rows
