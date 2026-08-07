from __future__ import annotations

import logging
import json
import time
from dataclasses import asdict
from importlib import import_module
from pathlib import Path
from typing import Any

import torch

from locomo_eval.compression.hybrid import HybridCompressor
from locomo_eval.compression.bm25 import BM25Compressor
from locomo_eval.compression.dense_retrieval import DenseRetrievalCompressor
from locomo_eval.compression.last_k_turns import LastKTurnsCompressor
from locomo_eval.compression.no_compression import NoCompressionCompressor
from locomo_eval.compression.neighbor_window import NeighborWindowCompressor
from locomo_eval.compression.oracle_evidence import OracleEvidenceCompressor
from locomo_eval.compression.retrieval import RetrievalCompressor
from locomo_eval.compression.session_summary import SessionSummaryCompressor
from locomo_eval.compression.sliding_window import SlidingWindowCompressor
from locomo_eval.experiments.results import load_completed, save_checkpoint, save_results
from locomo_eval.locomo.formatter import build_context_messages, make_format_and_count_fn
from locomo_eval.locomo.qa_builder import ConversationPrecomputed, build_qa_examples, precompute_retrieval
from locomo_eval.metrics.answer_metrics import score_answer
from locomo_eval.metrics.compression_metrics import compression_ratio, token_saving
from locomo_eval.metrics.evidence_metrics import answerable_context_rate, evidence_precision, evidence_recall, evidence_session_recall
from locomo_eval.metrics.perplexity_metrics import summarize_perplexity

LOGGER = logging.getLogger(__name__)


class CompressionBudgetExceeded(Exception):
    def __init__(self, compressed) -> None:
        self.compressed = compressed
        message = compressed.metadata.get(
            "budget_error",
            "compressed context exceeds the configured token budget",
        )
        super().__init__(message)


EXPERIMENTAL_COMPRESSOR_FACTORIES = {
    "claude_context": "locomo_eval.experimental.claude_context:build_claude_context_compressor",
    "claude_server_context": "locomo_eval.experimental.claude_context:build_claude_context_compressor",
}


class ExperimentRunner:
    def __init__(self, config, model_adapter) -> None:
        self.config = config
        self.model_adapter = model_adapter
        self._dense_retrieval_cache: dict[tuple[str, str], DenseRetrievalCompressor] = {}

    def _build_compressor(self, method: str, precomputed: ConversationPrecomputed, evidence_ids: list[str]):
        experimental = self._build_experimental_compressor(method)
        if experimental is not None:
            return experimental
        if method == "no_compression":
            return NoCompressionCompressor()
        if method == "last_k_turns":
            return LastKTurnsCompressor(self.config.last_k_default)
        if method == "sliding_window":
            return SlidingWindowCompressor()
        if method == "retrieval":
            return RetrievalCompressor(precomputed, top_k=self.config.retrieval_top_k, candidate_k=self.config.retrieval_candidate_k)
        if method == "retrieval_window":
            return NeighborWindowCompressor(
                RetrievalCompressor(precomputed, top_k=self.config.retrieval_top_k, candidate_k=self.config.retrieval_candidate_k),
                window_size=self.config.neighbor_window_size,
            )
        if method == "bm25":
            return BM25Compressor(
                precomputed.conversation.all_turns,
                top_k=self.config.retrieval_top_k,
                candidate_k=self.config.retrieval_candidate_k,
            )
        if method == "bm25_window":
            return NeighborWindowCompressor(
                BM25Compressor(
                    precomputed.conversation.all_turns,
                    top_k=self.config.retrieval_top_k,
                    candidate_k=self.config.retrieval_candidate_k,
                ),
                window_size=self.config.neighbor_window_size,
            )
        if method == "dense_retrieval":
            cache_key = (precomputed.conversation.conversation_id, self.config.dense_retrieval_model)
            if cache_key not in self._dense_retrieval_cache:
                self._dense_retrieval_cache[cache_key] = DenseRetrievalCompressor(
                    precomputed.conversation.all_turns,
                    model_name=self.config.dense_retrieval_model,
                    top_k=self.config.retrieval_top_k,
                    candidate_k=self.config.retrieval_candidate_k,
                )
            return self._dense_retrieval_cache[cache_key]
        if method == "dense_retrieval_window":
            cache_key = (precomputed.conversation.conversation_id, self.config.dense_retrieval_model)
            if cache_key not in self._dense_retrieval_cache:
                self._dense_retrieval_cache[cache_key] = DenseRetrievalCompressor(
                    precomputed.conversation.all_turns,
                    model_name=self.config.dense_retrieval_model,
                    top_k=self.config.retrieval_top_k,
                    candidate_k=self.config.retrieval_candidate_k,
                )
            return NeighborWindowCompressor(
                self._dense_retrieval_cache[cache_key],
                window_size=self.config.neighbor_window_size,
            )
        if method == "hybrid":
            hybrid_alloc = self.config.hybrid_allocation
            recent_k = int(hybrid_alloc.get("recent_ratio", 0.3) * 10)
            return HybridCompressor(
                retrieval_compressor=RetrievalCompressor(precomputed, top_k=self.config.retrieval_top_k, candidate_k=self.config.retrieval_candidate_k),
                recent_k=recent_k,
            )
        if method == "oracle_evidence":
            return OracleEvidenceCompressor(evidence_ids)
        if method == "session_summary":
            summary_text = "\n".join(session.summary or "" for session in precomputed.conversation.sessions if session.summary)
            return SessionSummaryCompressor(summary_text=summary_text)
        raise ValueError(f"Unknown compression method: {method}")

    def _build_experimental_compressor(self, method: str):
        target = EXPERIMENTAL_COMPRESSOR_FACTORIES.get(method)
        if target is None:
            return None
        module_name, factory_name = target.split(":", 1)
        module = import_module(module_name)
        factory = getattr(module, factory_name)
        return factory(self.config.method_options.get(method, {}))

    def _build_work_items(self, conversations: list) -> list[tuple[Any, ConversationPrecomputed, Any]]:
        by_conversation: list[tuple[Any, ConversationPrecomputed, list]] = []
        for conversation in conversations:
            precomputed = precompute_retrieval(conversation)
            by_conversation.append((conversation, precomputed, build_qa_examples([conversation])))

        if self.config.sample_strategy != "stratified":
            items = [(conversation, precomputed, qa) for conversation, precomputed, qas in by_conversation for qa in qas]
            return items[: self.config.max_samples] if self.config.max_samples else items

        buckets: dict[tuple[str, int], list[tuple[Any, ConversationPrecomputed, Any]]] = {}
        for conversation, precomputed, qas in by_conversation:
            for qa in qas:
                buckets.setdefault((conversation.conversation_id, qa.category), []).append((conversation, precomputed, qa))

        items: list[tuple[Any, ConversationPrecomputed, Any]] = []
        while buckets and (not self.config.max_samples or len(items) < self.config.max_samples):
            for key in sorted(list(buckets.keys())):
                bucket = buckets[key]
                if bucket:
                    items.append(bucket.pop(0))
                    if self.config.max_samples and len(items) >= self.config.max_samples:
                        break
                if not bucket:
                    del buckets[key]
        return items

    def _should_capture_attention(self, method: str, budget_raw: Any, captured_counts: dict[str, int]) -> bool:
        if self.config.attention_examples_per_method <= 0:
            return False
        budget_label = str(budget_raw)
        allowed_budget_labels = {str(item) for item in self.config.attention_budget_labels}
        if allowed_budget_labels and budget_label not in allowed_budget_labels:
            return False
        return captured_counts.get(method, 0) < self.config.attention_examples_per_method

    def run(self, conversations: list, dry_run: bool = False, shard_index: int = 0, num_shards: int = 1) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        completed = load_completed(self.config.output_dir)
        captured_attention_counts: dict[str, int] = {}

        work_items = self._build_work_items(conversations)
        if num_shards > 1:
            work_items = [item for i, item in enumerate(work_items) if i % num_shards == shard_index]
            LOGGER.info("Shard %d/%d: %d work items", shard_index, num_shards, len(work_items))

        for qa_index, (conversation, precomputed, qa) in enumerate(work_items):
                format_and_count_fn = make_format_and_count_fn(
                    tokenizer=self.model_adapter.tokenizer,
                    speaker_a=conversation.speaker_a,
                    speaker_b=conversation.speaker_b,
                    context_format=self.config.context_format,
                )
                original_text, original_tokens = format_and_count_fn(conversation.all_turns, None)

                for method in self.config.compression_methods:
                    compressor = self._build_compressor(method, precomputed, qa.evidence_ids)
                    for budget_raw in self.config.budgets:
                        budget = self.config.resolve_budget(budget_raw, original_tokens)
                        row_key = (conversation.conversation_id, qa.question_id, method, str(budget_raw))
                        if row_key in completed:
                            continue

                        try:
                            compressed = compressor.compress(conversation.all_turns, qa.question, budget, format_and_count_fn)
                            if compressed.metadata.get("budget_satisfied") is False:
                                raise CompressionBudgetExceeded(compressed)
                            messages = build_context_messages(
                                compressed.kept_turns,
                                conversation.speaker_a,
                                conversation.speaker_b,
                                system_prompt=self.config.system_prompt,
                                extra_context=compressed.extra_context,
                                context_format=self.config.context_format,
                            )
                            messages.append({"role": "user", "content": qa.question})

                            t0 = time.perf_counter()
                            generation = self.model_adapter.generate(messages, max_new_tokens=32 if dry_run else 256)
                            latency = time.perf_counter() - t0

                            answer_scores = score_answer(generation.text, qa.answer)
                            judge_scores: dict[str, Any] = {}
                            if self.config.enable_llm_judge and hasattr(self.model_adapter, "judge_answer"):
                                try:
                                    judge_scores = self.model_adapter.judge_answer(qa.question, qa.answer, generation.text)
                                except Exception:
                                    LOGGER.warning("LLM judge failed for %s/%s", qa.conversation_id, qa.question_id, exc_info=True)
                                    judge_scores = {
                                        "llm_judge_correct": None,
                                        "llm_judge_score": None,
                                        "llm_judge_rationale": "judge_failed",
                                    }

                            perplexity_payload: dict[str, Any] = {}
                            try:
                                perplexity_payload = self.model_adapter.compute_perplexity(messages, qa.answer)
                            except Exception:
                                LOGGER.warning("Perplexity computation failed for %s/%s", qa.conversation_id, qa.question_id, exc_info=True)
                                if torch.cuda.is_available():
                                    torch.cuda.empty_cache()
                            perplexity_summary = summarize_perplexity(perplexity_payload)

                            attention_payload: dict[str, Any] | None = None
                            if self._should_capture_attention(method, budget_raw, captured_attention_counts):
                                try:
                                    attention_payload = self.model_adapter.extract_attention(
                                        messages,
                                        target_layers=tuple(self.config.attention_layers),
                                    )
                                    captured_attention_counts[method] = captured_attention_counts.get(method, 0) + 1
                                except Exception:
                                    LOGGER.warning("Attention extraction failed for %s/%s", qa.conversation_id, qa.question_id, exc_info=True)
                                    attention_payload = {"status": "failed"}

                            gpu_memory = 0
                            if torch.cuda.is_available():
                                gpu_memory = torch.cuda.max_memory_allocated() // (1024 * 1024)

                            rows.append(
                                {
                                    "conversation_id": conversation.conversation_id,
                                    "question_id": qa.question_id,
                                    "question": qa.question,
                                    "category": qa.category,
                                    "method": method,
                                    "budget": budget,
                                    "budget_label": str(budget_raw),
                                    "prediction": generation.text,
                                    "reference": qa.answer,
                                    "evidence_ids": qa.evidence_ids,
                                    "kept_turn_ids": compressed.kept_turn_ids,
                                    "extra_context": compressed.extra_context,
                                    "compression_metadata": json.dumps(compressed.metadata, sort_keys=True),
                                    "compression_budget_satisfied": compressed.metadata.get(
                                        "budget_satisfied"
                                    ),
                                    "compression_error": compressed.metadata.get(
                                        "budget_error"
                                    ),
                                    "valid_for_analysis": True,
                                    "error": None,
                                    "original_tokens": original_tokens,
                                    "compressed_tokens": compressed.token_count,
                                    "compression_ratio": compression_ratio(original_tokens, compressed.token_count),
                                    "token_saving": token_saving(original_tokens, compressed.token_count),
                                    "evidence_recall": evidence_recall(compressed.kept_turn_ids, qa.evidence_ids),
                                    "evidence_precision": evidence_precision(compressed.kept_turn_ids, qa.evidence_ids),
                                    "evidence_session_recall": evidence_session_recall(compressed.kept_turns, conversation.all_turns, qa.evidence_ids),
                                    "answerable_context_rate": answerable_context_rate(compressed.kept_turns, qa.answer, compressed.extra_context),
                                    "perplexity": perplexity_summary.get("perplexity"),
                                    "nll": perplexity_summary.get("nll"),
                                    "token_nlls": perplexity_summary.get("token_nlls"),
                                    "answer_tokens": perplexity_summary.get("answer_tokens"),
                                    "scored_answer_tokens": perplexity_summary.get("scored_answer_tokens"),
                                    "answer_tokens_total": perplexity_summary.get("answer_tokens_total"),
                                    "context_tokens_used": perplexity_summary.get("context_tokens_used"),
                                    "context_tokens_dropped": perplexity_summary.get("context_tokens_dropped"),
                                    "attention_maps": json.dumps(attention_payload) if attention_payload else None,
                                    "latency": round(latency, 4),
                                    "gpu_memory_mb": gpu_memory,
                                    **answer_scores,
                                    **judge_scores,
                                }
                            )
                        except CompressionBudgetExceeded as exc:
                            compressed = exc.compressed
                            LOGGER.error(
                                "Compression budget exceeded for %s/%s "
                                "method=%s budget=%s: %s",
                                conversation.conversation_id,
                                qa.question_id,
                                method,
                                budget,
                                exc,
                            )
                            rows.append(
                                {
                                    "conversation_id": conversation.conversation_id,
                                    "question_id": qa.question_id,
                                    "question": qa.question,
                                    "category": qa.category,
                                    "method": method,
                                    "budget": budget,
                                    "budget_label": str(budget_raw),
                                    "prediction": None,
                                    "reference": qa.answer,
                                    "evidence_ids": qa.evidence_ids,
                                    "kept_turn_ids": compressed.kept_turn_ids,
                                    "extra_context": compressed.extra_context,
                                    "compression_metadata": json.dumps(
                                        compressed.metadata,
                                        sort_keys=True,
                                    ),
                                    "compression_budget_satisfied": False,
                                    "compression_error": str(exc),
                                    "valid_for_analysis": False,
                                    "original_tokens": original_tokens,
                                    "compressed_tokens": compressed.token_count,
                                    "compression_ratio": None,
                                    "token_saving": None,
                                    "evidence_recall": None,
                                    "evidence_precision": None,
                                    "evidence_session_recall": None,
                                    "answerable_context_rate": None,
                                    "perplexity": None,
                                    "nll": None,
                                    "token_nlls": [],
                                    "answer_tokens": [],
                                    "scored_answer_tokens": 0,
                                    "answer_tokens_total": 0,
                                    "context_tokens_used": 0,
                                    "context_tokens_dropped": 0,
                                    "attention_maps": None,
                                    "latency": None,
                                    "gpu_memory_mb": None,
                                    "token_f1": None,
                                    "rouge_l": None,
                                    "exact_match": None,
                                    "date_f1": None,
                                    "number_f1": None,
                                    "entity_f1": None,
                                    "llm_judge_correct": None,
                                    "llm_judge_score": None,
                                    "llm_judge_rationale": None,
                                    "error": (
                                        "COMPRESSION_BUDGET_EXCEEDED: "
                                        f"{exc}"
                                    ),
                                }
                            )
                        except (torch.cuda.OutOfMemoryError, RuntimeError) as exc:
                            if isinstance(exc, RuntimeError) and "CUDA" not in str(exc) and "cuda" not in str(exc):
                                raise
                            LOGGER.error("CUDA error for %s/%s method=%s budget=%s — skipping", conversation.conversation_id, qa.question_id, method, budget, exc_info=True)
                            if torch.cuda.is_available():
                                torch.cuda.empty_cache()
                            rows.append(
                                {
                                    "conversation_id": conversation.conversation_id,
                                    "question_id": qa.question_id,
                                    "question": qa.question,
                                    "category": qa.category,
                                    "method": method,
                                    "budget": budget,
                                    "budget_label": str(budget_raw),
                                    "prediction": None,
                                    "reference": qa.answer,
                                    "evidence_ids": qa.evidence_ids,
                                    "kept_turn_ids": [],
                                    "extra_context": None,
                                    "compression_metadata": None,
                                    "compression_budget_satisfied": None,
                                    "compression_error": None,
                                    "valid_for_analysis": False,
                                    "original_tokens": original_tokens,
                                    "compressed_tokens": 0,
                                    "compression_ratio": None,
                                    "token_saving": None,
                                    "evidence_recall": None,
                                    "evidence_precision": None,
                                    "evidence_session_recall": None,
                                    "answerable_context_rate": None,
                                    "perplexity": None,
                                    "nll": None,
                                    "token_nlls": [],
                                    "answer_tokens": [],
                                    "scored_answer_tokens": 0,
                                    "answer_tokens_total": 0,
                                    "context_tokens_used": 0,
                                    "context_tokens_dropped": 0,
                                    "attention_maps": None,
                                    "latency": None,
                                    "gpu_memory_mb": None,
                                    "token_f1": None,
                                    "rouge_l": None,
                                    "exact_match": None,
                                    "date_f1": None,
                                    "number_f1": None,
                                    "entity_f1": None,
                                    "llm_judge_correct": None,
                                    "llm_judge_score": None,
                                    "llm_judge_rationale": None,
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

        save_results(rows, self.config.output_dir)
        return rows
