from __future__ import annotations

import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer

from .schemas import Conversation, QAExample


@dataclass
class ConversationPrecomputed:
    conversation: Conversation
    qa_examples: list[QAExample]
    tfidf_matrix: Any
    tfidf_vectorizer: TfidfVectorizer
    turn_texts: list[str]


def build_qa_examples(conversations: list[Conversation], allowed_categories: set[int] | None = None) -> list[QAExample]:
    allowed = allowed_categories or {1, 2, 3, 4}
    examples: list[QAExample] = []
    for conversation in conversations:
        for qa in conversation.qa_examples:
            if qa.category in allowed:
                examples.append(qa)
    return examples


def precompute_retrieval(conversation: Conversation) -> ConversationPrecomputed:
    turn_texts = [turn.text for turn in conversation.all_turns]
    vectorizer = TfidfVectorizer(stop_words="english")
    matrix = vectorizer.fit_transform(turn_texts if turn_texts else [""])
    return ConversationPrecomputed(
        conversation=conversation,
        qa_examples=build_qa_examples([conversation]),
        tfidf_matrix=matrix,
        tfidf_vectorizer=vectorizer,
        turn_texts=turn_texts,
    )


def save_precomputed(conversation_precomputed: ConversationPrecomputed, output_dir: str | Path) -> Path:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    file_path = output_path / f"{conversation_precomputed.conversation.conversation_id}_precomputed.pkl"
    with file_path.open("wb") as handle:
        pickle.dump(conversation_precomputed, handle)
    return file_path


def load_precomputed(file_path: str | Path) -> ConversationPrecomputed:
    with Path(file_path).open("rb") as handle:
        return pickle.load(handle)


def conversations_to_frame(conversations: list[Conversation]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for conversation in conversations:
        for qa in build_qa_examples([conversation]):
            rows.append(
                {
                    "conversation_id": conversation.conversation_id,
                    "question_id": qa.question_id,
                    "question": qa.question,
                    "answer": qa.answer,
                    "category": qa.category,
                    "evidence_ids": qa.evidence_ids,
                }
            )
    return pd.DataFrame(rows)
