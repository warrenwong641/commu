from __future__ import annotations

import logging
from collections.abc import Callable

from .schemas import Turn

LOGGER = logging.getLogger(__name__)


def build_chat_messages(
    turns: list[Turn],
    speaker_a: str,
    speaker_b: str,
    system_prompt: str | None = None,
    extra_messages: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    speaker_to_role = {speaker_a: "user", speaker_b: "assistant"}
    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})

    last_speaker: str | None = None
    for turn in turns:
        role = speaker_to_role.get(turn.speaker)
        if role is None:
            raise ValueError(
                f"Turn {turn.dia_id}: speaker '{turn.speaker}' does not match "
                f"speaker_a='{speaker_a}' or speaker_b='{speaker_b}'"
            )
        if last_speaker == turn.speaker:
            LOGGER.warning("Consecutive same-speaker turns detected in %s", turn.dia_id)
        messages.append({"role": role, "content": turn.text})
        last_speaker = turn.speaker

    if extra_messages:
        messages.extend(extra_messages)
    return messages


def format_turn_as_evidence(turn: Turn) -> str:
    fields = []
    if turn.session_id:
        fields.append(f"session={turn.session_id}")
    if turn.timestamp:
        fields.append(f"date={turn.timestamp}")
    fields.append(f"turn={turn.dia_id}")
    fields.append(f"speaker={turn.speaker}")
    header = " ".join(fields)
    parts = [f"[{header}]\n{turn.text}"]
    if turn.blip_caption:
        parts.append(f"Image caption: {turn.blip_caption}")
    if turn.img_url:
        parts.append(f"Image URL: {turn.img_url}")
    return "\n".join(parts)


def build_evidence_messages(
    turns: list[Turn],
    speaker_a: str,
    speaker_b: str,
    system_prompt: str | None = None,
    extra_context: str | None = None,
) -> list[dict[str, str]]:
    del speaker_a, speaker_b
    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    blocks = [format_turn_as_evidence(turn) for turn in turns]
    if not blocks:
        blocks = ["[No retained turns]"]
    content = "Conversation history:\n\n" + "\n\n".join(blocks)
    if extra_context:
        content += "\n\nAdditional memory:\n\n" + extra_context
    messages.append({"role": "user", "content": content})
    return messages


def build_context_messages(
    turns: list[Turn],
    speaker_a: str,
    speaker_b: str,
    system_prompt: str | None = None,
    extra_context: str | None = None,
    context_format: str = "chat",
) -> list[dict[str, str]]:
    if context_format == "evidence":
        return build_evidence_messages(turns, speaker_a, speaker_b, system_prompt, extra_context)
    messages = build_chat_messages(turns, speaker_a, speaker_b, system_prompt=system_prompt)
    if extra_context:
        messages.insert(1, {"role": "system", "content": extra_context})
    return messages


def _fallback_template(messages: list[dict[str, str]]) -> str:
    return "".join(f"<|im_start|>{message['role']}\n{message['content']}<|im_end|>\n" for message in messages)


def make_format_and_count_fn(
    tokenizer,
    speaker_a: str,
    speaker_b: str,
    context_format: str = "chat",
) -> Callable[[list[Turn], str | None], tuple[str, int]]:
    cache: dict[tuple[tuple[str, ...], str], tuple[str, int]] = {}

    def fn(turns: list[Turn], extra_text: str | None = None) -> tuple[str, int]:
        key = (tuple(turn.dia_id for turn in turns), extra_text or "")
        if key in cache:
            return cache[key]
        messages = build_context_messages(
            turns,
            speaker_a,
            speaker_b,
            extra_context=extra_text,
            context_format=context_format,
        )
        if hasattr(tokenizer, "apply_chat_template"):
            text = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=False)
        else:
            text = _fallback_template(messages)
        tokenized = tokenizer(text, return_tensors="pt") if callable(tokenizer) else {"input_ids": [[0] * len(text.split())]}
        token_count = int(tokenized["input_ids"].shape[1] if hasattr(tokenized["input_ids"], "shape") else len(tokenized["input_ids"][0]))
        cache[key] = (text, token_count)
        return cache[key]

    return fn
