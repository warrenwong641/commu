from __future__ import annotations

import random
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from locomo_eval.locomo.formatter import format_turn_as_evidence
from locomo_eval.locomo.loader import load_conversations
from locomo_eval.locomo.schemas import Conversation, QAExample

from .common import read_jsonl, sha256_json, utc_now, write_jsonl

DEFAULT_SYSTEM_PROMPT = (
    "Answer the question using only the supplied conversation history. "
    "Give one short phrase unless a full sentence is necessary."
)
SUMMARY_SYSTEM_PROMPT = (
    "Summarize only significant events supported by the supplied conversation. "
    "Order events chronologically, preserve dates when available, and do not invent details."
)

SUPPORTED_CONDITIONS = {
    "no_compression": 1.0,
    "longllmlingua_2x": 0.5,
    "longllmlingua_4x": 0.25,
}


def _require_new_manifest_output(output_path: Path) -> None:
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    for candidate in (output_path, temporary):
        if candidate.exists() or candidate.is_symlink():
            raise FileExistsError(
                f"refusing to overwrite frozen manifest artifact: {candidate}"
            )


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


def _event_reference(conversation: Conversation, speaker: str) -> str:
    def reference_strings(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [
                text
                for item in value
                for text in reference_strings(item)
            ]
        if isinstance(value, dict):
            return [
                text
                for item in value.values()
                for text in reference_strings(item)
            ]
        return [str(value)] if value is not None else []

    references: list[str] = []
    speaker_lower = speaker.casefold()
    aliases = {speaker_lower}
    if speaker == conversation.speaker_a:
        aliases.add("speaker_a")
    if speaker == conversation.speaker_b:
        aliases.add("speaker_b")
    for session in conversation.sessions:
        value = session.event_summary
        if isinstance(value, dict):
            matching = [item for key, item in value.items() if str(key).casefold() in aliases]
            if not matching:
                matching = [
                    item for item in value.values() if speaker_lower in str(item).casefold()
                ]
            references.extend(
                text
                for item in matching
                for text in reference_strings(item)
                if text
            )
        elif isinstance(value, list):
            references.extend(
                text
                for item in value
                if speaker_lower in str(item).casefold()
                for text in reference_strings(item)
                if text
            )
        elif value:
            references.extend(reference_strings(value))
    return "\n".join(dict.fromkeys(references))


def _compress_context(
    *,
    blocks: list[str],
    condition: str,
    compressor: Any,
    compressor_model: str,
    task_instruction: str,
) -> tuple[str, dict[str, Any]]:
    rate = SUPPORTED_CONDITIONS[condition]
    if condition == "no_compression":
        return "\n\n".join(blocks), {
            "method": "none",
            "target_rate": 1.0,
            "origin_tokens": None,
            "compressed_tokens": None,
            "reported_ratio": "1.0x",
        }
    result = compressor.compress_prompt(
        blocks,
        question=task_instruction,
        rate=rate,
        condition_in_question="after_condition",
        reorder_context="sort",
        dynamic_context_compression_ratio=0.3,
        condition_compare=True,
        context_budget="+100",
        rank_method="longllmlingua",
        concate_question=False,
    )
    return str(result["compressed_prompt"]), {
        "method": "LongLLMLingua",
        "compressor_model": compressor_model,
        "target_rate": rate,
        "origin_tokens": result.get("origin_tokens"),
        "compressed_tokens": result.get("compressed_tokens"),
        "reported_ratio": result.get("ratio"),
        "reported_rate": result.get("rate"),
    }


def _load_compressor(model_name: str, device_map: str):
    try:
        from llmlingua import PromptCompressor
    except ImportError as exc:
        raise RuntimeError(
            "LongLLMLingua is required for compressed conditions. "
            "Run traffic_experiment/scripts/01_setup_runner.sh, then use "
            "traffic_experiment/.venv-compression/bin/python for preparation."
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
    shard_count: int = 1,
    shard_index: int = 0,
) -> list[dict[str, Any]]:
    _require_new_manifest_output(output_path)
    unknown = sorted(set(conditions) - set(SUPPORTED_CONDITIONS))
    if unknown:
        raise ValueError(f"unsupported condition(s): {', '.join(unknown)}")

    conversations = load_conversations(data_dir)
    if shard_count <= 0:
        raise ValueError("shard count must be positive")
    if shard_index < 0 or shard_index >= shard_count:
        raise ValueError(f"shard index must be in [0, {shard_count})")

    selected = list(enumerate(select_examples(conversations, sample_count, seed)))
    selected = selected[shard_index::shard_count]
    requires_compressor = any(name != "no_compression" for name in conditions)
    compressor = _load_compressor(compressor_model, compressor_device) if requires_compressor else None

    rows: list[dict[str, Any]] = []
    prepared_at = utc_now()
    for selection_index, (conversation, qa) in selected:
        blocks = _context_blocks(conversation)
        sample_id = f"{conversation.conversation_id}::{qa.question_id}"

        for condition in conditions:
            compression_started = time.perf_counter()
            context, compression_metadata = _compress_context(
                blocks=blocks,
                condition=condition,
                compressor=compressor,
                compressor_model=compressor_model,
                task_instruction=qa.question,
            )

            messages = _build_messages(system_prompt, context, qa.question)
            compression_metadata["elapsed_seconds"] = round(time.perf_counter() - compression_started, 6)
            row = {
                "manifest_version": 1,
                "task_type": "qa",
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
                "selection_index": selection_index,
                "preparation_shard": {
                    "index": shard_index,
                    "count": shard_count,
                },
            }
            rows.append(row)

    write_jsonl(output_path, rows)
    return rows


def prepare_summary_manifest(
    data_dir: Path,
    output_path: Path,
    conversation_count: int,
    seed: int,
    conditions: list[str],
    compressor_model: str,
    compressor_device: str,
    system_prompt: str = SUMMARY_SYSTEM_PROMPT,
) -> list[dict[str, Any]]:
    _require_new_manifest_output(output_path)
    unknown = sorted(set(conditions) - set(SUPPORTED_CONDITIONS))
    if unknown:
        raise ValueError(f"unsupported condition(s): {', '.join(unknown)}")
    conversations = load_conversations(data_dir)
    eligible = [
        conversation
        for conversation in conversations
        if any(session.event_summary for session in conversation.sessions)
    ]
    if conversation_count <= 0:
        raise ValueError("conversation count must be positive")
    if len(eligible) < conversation_count:
        raise ValueError(
            f"requested {conversation_count} conversations, but only {len(eligible)} have event summaries"
        )
    selected = random.Random(seed).sample(eligible, conversation_count)
    selected.sort(key=lambda conversation: conversation.conversation_id)
    compressor = (
        _load_compressor(compressor_model, compressor_device)
        if any(name != "no_compression" for name in conditions)
        else None
    )

    rows: list[dict[str, Any]] = []
    prepared_at = utc_now()
    for selection_index, conversation in enumerate(selected):
        blocks = _context_blocks(conversation)
        for speaker in (conversation.speaker_a, conversation.speaker_b):
            reference = _event_reference(conversation, speaker)
            if not reference:
                continue
            instruction = f"Summarize significant events for {speaker} chronologically."
            sample_id = f"{conversation.conversation_id}::event_summary::{speaker}"
            for condition in conditions:
                started = time.perf_counter()
                context, compression = _compress_context(
                    blocks=blocks,
                    condition=condition,
                    compressor=compressor,
                    compressor_model=compressor_model,
                    task_instruction=instruction,
                )
                messages = _build_messages(system_prompt, context, instruction)
                compression["elapsed_seconds"] = round(time.perf_counter() - started, 6)
                rows.append(
                    {
                        "manifest_version": 1,
                        "task_type": "event_summary",
                        "request_id": f"{sample_id}::{condition}",
                        "sample_id": sample_id,
                        "conversation_id": conversation.conversation_id,
                        "question_id": None,
                        "target_speaker": speaker,
                        "condition": condition,
                        "question": instruction,
                        "reference_answer": reference,
                        "messages": messages,
                        "messages_sha256": sha256_json(messages),
                        "compression": compression,
                        "prepared_at_utc": prepared_at,
                        "selection_seed": seed,
                        "selection_index": selection_index,
                    }
                )
    if not rows:
        raise ValueError("event-summary annotations were present but no speaker references could be derived")
    write_jsonl(output_path, rows)
    return rows


def merge_manifest_shards(
    input_paths: list[Path],
    output_path: Path,
    expected_rows: int | None = None,
) -> list[dict[str, Any]]:
    _require_new_manifest_output(output_path)
    if not input_paths:
        raise ValueError("at least one input manifest is required")

    rows: list[dict[str, Any]] = []
    seen_request_ids: set[str] = set()
    for input_path in input_paths:
        for row in read_jsonl(input_path):
            request_id = str(row.get("request_id", ""))
            if not request_id:
                raise ValueError(f"{input_path}: row is missing request_id")
            if request_id in seen_request_ids:
                raise ValueError(f"duplicate request_id across shards: {request_id}")
            if not isinstance(row.get("selection_index"), int):
                raise ValueError(f"{input_path}: {request_id} is missing integer selection_index")
            seen_request_ids.add(request_id)
            rows.append(row)

    condition_order = {name: index for index, name in enumerate(SUPPORTED_CONDITIONS)}
    rows.sort(
        key=lambda row: (
            int(row["selection_index"]),
            condition_order.get(str(row.get("condition")), len(condition_order)),
            str(row["request_id"]),
        )
    )
    if expected_rows is not None and len(rows) != expected_rows:
        raise ValueError(f"expected {expected_rows} rows across shards, found {len(rows)}")

    write_jsonl(output_path, rows)
    return rows
