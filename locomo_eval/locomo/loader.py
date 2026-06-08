from __future__ import annotations

import json
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
    return QAExample(
        question_id=str(raw.get("question_id", index)),
        conversation_id=conversation_id,
        question=raw["question"],
        answer=raw["answer"],
        evidence_ids=[str(item) for item in raw.get("evidence", [])],
        category=int(raw["category"]),
        metadata={k: v for k, v in raw.items() if k not in {"question_id", "question", "answer", "evidence", "category"}},
    )


def parse_conversation_record(raw: dict, source_name: str = "") -> Conversation:
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
