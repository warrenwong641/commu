from __future__ import annotations

import logging
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import torch

from locomo_eval.compression.hybrid import HybridCompressor
from locomo_eval.compression.last_k_turns import LastKTurnsCompressor
from locomo_eval.compression.no_compression import NoCompressionCompressor
from locomo_eval.compression.oracle_evidence import OracleEvidenceCompressor
from locomo_eval.compression.retrieval import RetrievalCompressor
from locomo_eval.compression.session_summary import SessionSummaryCompressor
from locomo_eval.compression.sliding_window import SlidingWindowCompressor
from locomo_eval.experiments.results import load_completed, save_checkpoint, save_results
from locomo_eval.locomo.formatter import build_chat_messages, make_format_and_count_fn
from locomo_eval.locomo.qa_builder import ConversationPrecomputed, build_qa_examples, precompute_retrieval
from locomo_eval.metrics.answer_metrics import score_answer
from locomo_eval.metrics.compression_metrics import compression_ratio, token_saving
from locomo_eval.metrics.evidence_metrics import evidence_precision, evidence_recall
from locomo_eval.metrics.perplexity_metrics import summarize_perplexity

LOGGER = logging.getLogger(__name__)


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
            hybrid_alloc = self.config.hybrid_allocation
            recent_k = int(hybrid_alloc.get("recent_ratio", 0.3) * 10)
            return HybridCompressor(
                retrieval_compressor=RetrievalCompressor(precomputed),
                recent_k=recent_k,
            )
        if method == "oracle_evidence":
            return OracleEvidenceCompressor(evidence_ids)
        if method == "session_summary":
            summary_text = "\n".join(session.summary or "" for session in precomputed.conversation.sessions if session.summary)
            return SessionSummaryCompressor(summary_text=summary_text)
        raise ValueError(f"Unknown compression method: {method}")

    def run(self, conversations: list, dry_run: bool = False) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        completed = load_completed(self.config.output_dir)
        qa_index = 0

        for conversation in conversations:
            precomputed = precompute_retrieval(conversation)
            qas = build_qa_examples([conversation])
            for qa in qas:
                if self.config.max_samples and qa_index >= self.config.max_samples:
                    save_results(rows, self.config.output_dir)
                    return rows

                format_and_count_fn = make_format_and_count_fn(
                    tokenizer=self.model_adapter.tokenizer,
                    speaker_a=conversation.speaker_a,
                    speaker_b=conversation.speaker_b,
                )
                original_text, original_tokens = format_and_count_fn(conversation.all_turns, None)

                for method in self.config.compression_methods:
                    compressor = self._build_compressor(method, precomputed, qa.evidence_ids)
                    for budget_raw in self.config.budgets:
                        budget = self.config.resolve_budget(budget_raw, original_tokens)
                        row_key = (conversation.conversation_id, qa.question_id, method, budget_raw)
                        if row_key in completed:
                            continue

                        try:
                            compressed = compressor.compress(conversation.all_turns, qa.question, budget, format_and_count_fn)
                            messages = build_chat_messages(
                                compressed.kept_turns,
                                conversation.speaker_a,
                                conversation.speaker_b,
                                system_prompt=self.config.system_prompt,
                            )
                            if compressed.extra_context:
                                messages.insert(1, {"role": "system", "content": compressed.extra_context})
                            messages.append({"role": "user", "content": qa.question})

                            t0 = time.perf_counter()
                            generation = self.model_adapter.generate(messages, max_new_tokens=32 if dry_run else 256)
                            latency = time.perf_counter() - t0

                            answer_scores = score_answer(generation.text, qa.answer)

                            perplexity_payload: dict[str, Any] = {}
                            try:
                                perplexity_payload = self.model_adapter.compute_perplexity(messages, qa.answer)
                            except Exception:
                                LOGGER.warning("Perplexity computation failed for %s/%s", qa.conversation_id, qa.question_id, exc_info=True)

                            gpu_memory = 0
                            if torch.cuda.is_available():
                                gpu_memory = torch.cuda.max_memory_allocated() // (1024 * 1024)

                            rows.append(
                                {
                                    "conversation_id": conversation.conversation_id,
                                    "question_id": qa.question_id,
                                    "method": method,
                                    "budget": budget,
                                    "budget_label": str(budget_raw),
                                    "prediction": generation.text,
                                    "reference": qa.answer,
                                    "evidence_ids": qa.evidence_ids,
                                    "kept_turn_ids": compressed.kept_turn_ids,
                                    "original_tokens": original_tokens,
                                    "compressed_tokens": compressed.token_count,
                                    "compression_ratio": compression_ratio(original_tokens, compressed.token_count),
                                    "token_saving": token_saving(original_tokens, compressed.token_count),
                                    "evidence_recall": evidence_recall(compressed.kept_turn_ids, qa.evidence_ids),
                                    "evidence_precision": evidence_precision(compressed.kept_turn_ids, qa.evidence_ids),
                                    "perplexity": summarize_perplexity(perplexity_payload).get("perplexity") if perplexity_payload else None,
                                    "nll": summarize_perplexity(perplexity_payload).get("nll") if perplexity_payload else None,
                                    "latency": round(latency, 4),
                                    "gpu_memory_mb": gpu_memory,
                                    **answer_scores,
                                }
                            )
                        except torch.cuda.OutOfMemoryError:
                            LOGGER.error("OOM for %s/%s method=%s budget=%s — skipping", conversation.conversation_id, qa.question_id, method, budget, exc_info=True)
                            rows.append(
                                {
                                    "conversation_id": conversation.conversation_id,
                                    "question_id": qa.question_id,
                                    "method": method,
                                    "budget": budget,
                                    "budget_label": str(budget_raw),
                                    "prediction": None,
                                    "reference": qa.answer,
                                    "evidence_ids": qa.evidence_ids,
                                    "kept_turn_ids": [],
                                    "original_tokens": original_tokens,
                                    "compressed_tokens": 0,
                                    "compression_ratio": None,
                                    "token_saving": None,
                                    "evidence_recall": None,
                                    "evidence_precision": None,
                                    "perplexity": None,
                                    "nll": None,
                                    "latency": None,
                                    "gpu_memory_mb": None,
                                    "token_f1": None,
                                    "rouge_l": None,
                                    "error": "OOM",
                                }
                            )

                        completed.add(row_key)
                        if len(rows) % 10 == 0:
                            save_checkpoint(
                                {
                                    "rows_completed": len(rows),
                                    "completed_keys": sorted(str(k) for k in completed),
                                },
                                self.config.output_dir,
                            )

                    if dry_run and len(rows) >= 3:
                        save_results(rows, self.config.output_dir)
                        return rows

                qa_index += 1

        save_results(rows, self.config.output_dir)
        return rows
