from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Turn:
    dia_id: str
    speaker: str
    text: str
    session_id: str | None = None
    timestamp: str | None = None
    blip_caption: str | None = None
    img_url: str | None = None


@dataclass
class Session:
    session_id: str
    timestamp: str | None
    turns: list[Turn]
    summary: str | None = None
    event_summary: str | None = None
    observations: list[str] = field(default_factory=list)


@dataclass
class QAExample:
    question_id: str
    conversation_id: str
    question: str
    answer: str
    evidence_ids: list[str]
    category: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Conversation:
    conversation_id: str
    speaker_a: str
    speaker_b: str
    sessions: list[Session]
    qa_examples: list[QAExample] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def all_turns(self) -> list[Turn]:
        turns: list[Turn] = []
        for session in self.sessions:
            turns.extend(session.turns)
        return turns
