from __future__ import annotations

import json
import re
from pathlib import Path

from .schemas import Conversation, QAExample, Session, Turn


def _parse_turn(raw: dict, session_id: str, timestamp: str | None) -> Turn:
    return Turn(
        dia_id=str(raw["dia_id"]),
        speaker=raw["speaker"],
        text=raw.get("text", ""),
        session_id=session_id,
        timestamp=timestamp,
        blip_caption=raw.get("blip_caption"),
        img_url=raw.get("img_url"),
    )


def _parse_session(raw: dict, index: int) -> Session:
    session_id = str(raw.get("session_id", index))
    timestamp = raw.get("timestamp")
    turns = [_parse_turn(turn, session_id, timestamp) for turn in raw.get("turns", [])]
    return Session(
        session_id=session_id,
        timestamp=timestamp,
        turns=turns,
        summary=raw.get("session_summary") or raw.get("summary"),
        event_summary=raw.get("event_summary"),
        observations=raw.get("observations", []) or [],
    )


def _parse_qa(raw: dict, conversation_id: str, index: int) -> QAExample:
    answer = str(raw.get("answer") or raw.get("adversarial_answer", ""))
    return QAExample(
        question_id=str(raw.get("question_id", index)),
        conversation_id=conversation_id,
        question=raw["question"],
        answer=answer,
        evidence_ids=[str(item) for item in raw.get("evidence", [])],
        category=int(raw["category"]),
        metadata={k: v for k, v in raw.items() if k not in {"question_id", "question", "answer", "adversarial_answer", "evidence", "category"}},
    )


def _session_sort_key(session_key: str) -> tuple[int, str]:
    match = re.match(r"session_(\d+)$", session_key)
    if match:
        return (int(match.group(1)), session_key)
    return (0, session_key)


def _normalize_session_id(session_key: str) -> str:
    return session_key.replace("session_", "s", 1)


def _parse_locomo_github_format(raw: dict, source_name: str = "") -> Conversation:
    conversation_block = raw["conversation"]
    conversation_id = str(raw.get("sample_id") or raw.get("conversation_id") or raw.get("id") or source_name)
    session_summary = raw.get("session_summary") or {}
    event_summary = raw.get("event_summary") or {}
    observation = raw.get("observation") or {}

    sessions: list[Session] = []
    session_keys = sorted(
        [
            key
            for key, value in conversation_block.items()
            if key.startswith("session_") and not key.endswith("_date_time") and isinstance(value, list)
        ],
        key=_session_sort_key,
    )

    for session_key in session_keys:
        session_id = _normalize_session_id(session_key)
        timestamp = conversation_block.get(f"{session_key}_date_time")
        turns = [_parse_turn(turn, session_id, timestamp) for turn in conversation_block.get(session_key, [])]
        summary_key = f"{session_key}_summary"
        event_key = f"{session_key}_summary"
        observation_values = observation.get(session_key, [])
        if isinstance(observation_values, str):
            observations = [observation_values]
        elif isinstance(observation_values, list):
            observations = [str(item) for item in observation_values]
        elif isinstance(observation_values, dict):
            observations = [str(value) for value in observation_values.values()]
        else:
            observations = []

        sessions.append(
            Session(
                session_id=session_id,
                timestamp=timestamp,
                turns=turns,
                summary=session_summary.get(summary_key),
                event_summary=event_summary.get(event_key),
                observations=observations,
            )
        )

    qas = [_parse_qa(qa, conversation_id, index) for index, qa in enumerate(raw.get("qa", []) or raw.get("qas", []), start=1)]
    return Conversation(
        conversation_id=conversation_id,
        speaker_a=conversation_block["speaker_a"],
        speaker_b=conversation_block["speaker_b"],
        sessions=sessions,
        qa_examples=qas,
        metadata={
            "source_format": "locomo_github",
            "session_summary": session_summary,
            "event_summary": event_summary,
            "observation": observation,
        },
    )


def parse_conversation_record(raw: dict, source_name: str = "") -> Conversation:
    if isinstance(raw.get("conversation"), dict) and "speaker_a" in raw["conversation"] and "speaker_b" in raw["conversation"]:
        return _parse_locomo_github_format(raw, source_name=source_name)

    conversation_id = str(raw.get("conversation_id") or raw.get("conv_id") or raw.get("id") or source_name)
    sessions = [_parse_session(session, index) for index, session in enumerate(raw.get("sessions", []), start=1)]
    qas = [_parse_qa(qa, conversation_id, index) for index, qa in enumerate(raw.get("qa", []) or raw.get("qas", []), start=1)]
    return Conversation(
        conversation_id=conversation_id,
        speaker_a=raw["speaker_a"],
        speaker_b=raw["speaker_b"],
        sessions=sessions,
        qa_examples=qas,
        metadata={k: v for k, v in raw.items() if k not in {"conversation_id", "conv_id", "id", "speaker_a", "speaker_b", "sessions", "qa", "qas"}},
    )


def load_conversations(data_dir: str | Path) -> list[Conversation]:
    path = Path(data_dir)
    if not path.exists():
        raise FileNotFoundError(f"Data directory does not exist: {path}")
    files = sorted(path.glob("*.json"))
    if not files:
        raise FileNotFoundError(f"No JSON files found in {path}")

    conversations: list[Conversation] = []
    for file_path in files:
        with file_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        if isinstance(raw, list):
            for index, item in enumerate(raw):
                conversations.append(parse_conversation_record(item, source_name=f"{file_path.stem}_{index}"))
        else:
            conversations.append(parse_conversation_record(raw, source_name=file_path.stem))
    return conversations
