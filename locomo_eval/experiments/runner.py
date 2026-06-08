from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from locomo_eval.compression.hybrid import HybridCompressor
from locomo_eval.compression.last_k_turns import LastKTurnsCompressor
from locomo_eval.compression.no_compression import NoCompressionCompressor
from locomo_eval.compression.oracle_evidence import OracleEvidenceCompressor
from locomo_eval.compression.retrieval import RetrievalCompressor
from locomo_eval.compression.session_summary import SessionSummaryCompressor
from locomo_eval.compression.sliding_window import SlidingWindowCompressor
from locomo_eval.experiments.results import save_checkpoint, save_results
from locomo_eval.locomo.formatter import build_chat_messages, make_format_and_count_fn
from locomo_eval.locomo.qa_builder import ConversationPrecomputed, build_qa_examples, precompute_retrieval
from locomo_eval.metrics.answer_metrics import score_answer
from locomo_eval.metrics.compression_metrics import compression_ratio
from locomo_eval.metrics.evidence_metrics import evidence_precision, evidence_recall


class ExperimentRunner:
    def __init__(self, config, model_adapter) -> None:
        self.config = config
        self.model_adapter = model_adapter

    def _build_compressor(self, method: str, precomputed: ConversationPrecomputed, evidence_ids: list[str]):
        if method == "no_compression":
            return NoCompressionCompressor()
        if method == "last_k_turns":
            return LastKTurnsCompressor(self.config.last_k_default)
        if method == "sliding_window":
            return SlidingWindowCompressor()
        if method == "retrieval":
            return RetrievalCompressor(precomputed)
        if method == "hybrid":
            return HybridCompressor(retrieval_compressor=RetrievalCompressor(precomputed))
        if method == "oracle_evidence":
            return OracleEvidenceCompressor(evidence_ids)
        if method == "session_summary":
            summary_text = "\n".join(session.summary or "" for session in precomputed.conversation.sessions if session.summary)
            return SessionSummaryCompressor(summary_text=summary_text)
        raise ValueError(f"Unknown compression method: {method}")

    def run(self, conversations: list, dry_run: bool = False) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        samples_seen = 0
        for conversation in conversations:
            precomputed = precompute_retrieval(conversation)
            qas = build_qa_examples([conversation])
            for qa in qas:
                format_and_count_fn = make_format_and_count_fn(
                    tokenizer=self.model_adapter.tokenizer,
                    speaker_a=conversation.speaker_a,
                    speaker_b=conversation.speaker_b,
                )
                original_text, original_tokens = format_and_count_fn(conversation.all_turns, None)
                for method in self.config.compression_methods:
                    compressor = self._build_compressor(method, precomputed, qa.evidence_ids)
                    for budget in self.config.budgets:
                        compressed = compressor.compress(conversation.all_turns, qa.question, budget, format_and_count_fn)
                        messages = build_chat_messages(
                            compressed.kept_turns,
                            conversation.speaker_a,
                            conversation.speaker_b,
                            system_prompt=self.config.system_prompt,
                            extra_messages=[{"role": "user", "content": qa.question}],
                        )
                        generation = self.model_adapter.generate(messages, max_new_tokens=32 if dry_run else 256)
                        answer_scores = score_answer(generation.text, qa.answer)
                        rows.append(
                            {
                                "conversation_id": conversation.conversation_id,
                                "question_id": qa.question_id,
                                "method": method,
                                "budget": budget,
                                "prediction": generation.text,
                                "reference": qa.answer,
                                "evidence_ids": qa.evidence_ids,
                                "kept_turn_ids": compressed.kept_turn_ids,
                                "original_tokens": original_tokens,
                                "compressed_tokens": compressed.token_count,
                                "compression_ratio": compression_ratio(original_tokens, compressed.token_count),
                                "evidence_recall": evidence_recall(compressed.kept_turn_ids, qa.evidence_ids),
                                "evidence_precision": evidence_precision(compressed.kept_turn_ids, qa.evidence_ids),
                                **answer_scores,
                            }
                        )
                        samples_seen += 1
                        if samples_seen % 10 == 0:
                            save_checkpoint({"rows_completed": samples_seen}, self.config.output_dir)
                        if self.config.max_samples and samples_seen >= self.config.max_samples:
                            save_results(rows, self.config.output_dir)
                            return rows
                        if dry_run and samples_seen >= 3:
                            save_results(rows, self.config.output_dir)
                            return rows
        save_results(rows, self.config.output_dir)
        return rows
