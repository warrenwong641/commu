from __future__ import annotations

import pytest

from locomo_eval.locomo.schemas import Conversation, QAExample, Session, Turn


@pytest.fixture()
def mock_conversation() -> Conversation:
    sessions = [
        Session(
            session_id="s1",
            timestamp="2024-01-01",
            turns=[
                Turn(dia_id="1", speaker="Alice", text="I live in Taipei."),
                Turn(dia_id="2", speaker="Bob", text="You said you live in Taipei."),
                Turn(dia_id="3", speaker="Alice", text="My favorite food is noodles."),
                Turn(dia_id="4", speaker="Bob", text="You like noodles."),
            ],
            summary="Alice lives in Taipei and likes noodles.",
        ),
        Session(
            session_id="s2",
            timestamp="2024-01-02",
            turns=[
                Turn(dia_id="5", speaker="Alice", text="I will travel on Friday."),
                Turn(dia_id="6", speaker="Bob", text="You travel on Friday."),
                Turn(dia_id="7", speaker="Alice", text="The train leaves at 8 AM."),
                Turn(dia_id="8", speaker="Bob", text="The train leaves at 8 AM."),
            ],
            summary="Alice plans to travel Friday at 8 AM.",
        ),
        Session(
            session_id="s3",
            timestamp="2024-01-03",
            turns=[
                Turn(dia_id="9", speaker="Alice", text="My brother is named Kevin."),
                Turn(dia_id="10", speaker="Bob", text="Your brother is Kevin."),
                Turn(dia_id="11", speaker="Alice", text="He works as a teacher."),
                Turn(dia_id="12", speaker="Bob", text="Kevin is a teacher."),
            ],
            summary="Kevin is Alice's brother and a teacher.",
        ),
    ]
    qas = [
        QAExample(question_id="q1", conversation_id="c1", question="Where does Alice live?", answer="Taipei", evidence_ids=["1"], category=1),
        QAExample(question_id="q2", conversation_id="c1", question="When does the train leave?", answer="8 AM", evidence_ids=["7"], category=2),
        QAExample(question_id="q3", conversation_id="c1", question="Adversarial sample", answer="N/A", evidence_ids=["12"], category=5),
    ]
    return Conversation(conversation_id="c1", speaker_a="Alice", speaker_b="Bob", sessions=sessions, qa_examples=qas)


@pytest.fixture()
def simple_tokenizer():
    class Tokenizer:
        def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=False):
            text = "".join(f"<|im_start|>{m['role']}\n{m['content']}<|im_end|>\n" for m in messages)
            if add_generation_prompt:
                text += "<|im_start|>assistant\n"
            return text

        def __call__(self, text, return_tensors="pt"):
            class Tensor:
                shape = (1, max(1, len(text.split())))
            return {"input_ids": Tensor()}

    return Tokenizer()
