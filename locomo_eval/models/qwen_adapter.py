from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import torch
import torch.nn.functional as F
from transformers import AutoModelForCausalLM, AutoTokenizer


@dataclass
class GenerationResult:
    text: str
    prompt_tokens: int
    completion_tokens: int
    metadata: dict[str, Any]


class QwenAdapter:
    def __init__(self, model_name: str, trust_remote_code: bool = True) -> None:
        self.model_name = model_name
        self.tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=trust_remote_code)
        model_kwargs = {
            "trust_remote_code": trust_remote_code,
            "device_map": "auto",
            "dtype": "auto",
            "attn_implementation": "eager",
        }
        try:
            self.model = AutoModelForCausalLM.from_pretrained(model_name, **model_kwargs)
        except TypeError:
            model_kwargs.pop("attn_implementation", None)
            self.model = AutoModelForCausalLM.from_pretrained(model_name, **model_kwargs)
        self.model.eval()

    def count_tokens(self, text: str) -> int:
        return int(self.tokenizer(text, return_tensors="pt").input_ids.shape[1])

    def generate(self, messages: list[dict[str, str]], max_new_tokens: int = 256) -> GenerationResult:
        text = self.tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = self.tokenizer(text, return_tensors="pt").to(self.model.device)
        with torch.no_grad():
            outputs = self.model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
                do_sample=False,
                temperature=None,
                top_p=None,
                top_k=None,
            )
        generated_ids = outputs[0][inputs.input_ids.shape[1]:]
        generated_text = self.tokenizer.decode(generated_ids, skip_special_tokens=True)
        return GenerationResult(
            text=generated_text.strip(),
            prompt_tokens=int(inputs.input_ids.shape[1]),
            completion_tokens=int(generated_ids.shape[0]),
            metadata={},
        )

    def judge_answer(self, question: str, reference: str, prediction: str) -> dict[str, Any]:
        prompt = (
            "You are evaluating whether a predicted answer is semantically correct.\n"
            "Return only compact JSON with keys: correct, score, rationale.\n"
            "correct must be true or false. score must be one of 0, 0.5, 1.\n\n"
            f"Question: {question}\n"
            f"Reference answer: {reference}\n"
            f"Predicted answer: {prediction}\n"
        )
        result = self.generate([{"role": "user", "content": prompt}], max_new_tokens=128)
        text = result.text.strip()
        try:
            import json

            start = text.find("{")
            end = text.rfind("}")
            payload = text[start : end + 1] if start >= 0 and end >= start else text
            parsed = json.loads(payload)
            return {
                "llm_judge_correct": bool(parsed.get("correct", False)),
                "llm_judge_score": float(parsed.get("score", 0.0)),
                "llm_judge_rationale": str(parsed.get("rationale", "")),
            }
        except Exception:
            return {
                "llm_judge_correct": None,
                "llm_judge_score": None,
                "llm_judge_rationale": text,
            }

    def compute_perplexity(self, messages: list[dict[str, str]], answer_text: str, max_context_tokens: int = 4096) -> dict[str, Any]:
        context_text = self.tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)

        prefix_ids = self.tokenizer(context_text, return_tensors="pt").input_ids[0]
        answer_ids = self.tokenizer(answer_text, return_tensors="pt", add_special_tokens=False).input_ids[0]
        if answer_ids.numel() == 0:
            return {
                "perplexity": None,
                "nll": None,
                "token_nlls": [],
                "answer_tokens": [],
                "scored_answer_tokens": 0,
                "context_tokens_used": 0,
                "context_tokens_dropped": int(prefix_ids.numel()),
            }

        answer_tokens_total = int(answer_ids.numel())
        if answer_tokens_total >= max_context_tokens:
            answer_ids = answer_ids[-(max_context_tokens - 1) :]

        max_prefix_tokens = max_context_tokens - int(answer_ids.numel())
        prefix_tokens_total = int(prefix_ids.numel())
        prefix_ids = prefix_ids[-max_prefix_tokens:] if max_prefix_tokens > 0 else prefix_ids[:0]
        context_len = int(prefix_ids.numel())

        full_ids = torch.cat([prefix_ids, answer_ids]).unsqueeze(0).to(self.model.device)

        labels = full_ids.clone()
        labels[:, :context_len] = -100

        with torch.no_grad():
            outputs = self.model(input_ids=full_ids, labels=labels)
        nll = float(outputs.loss.item())
        logits = outputs.logits

        if context_len >= full_ids.shape[1]:
            return {
                "perplexity": None,
                "nll": None,
                "token_nlls": [],
                "answer_tokens": [],
                "scored_answer_tokens": 0,
                "context_tokens_used": context_len,
                "context_tokens_dropped": max(0, prefix_tokens_total - context_len),
            }

        shift_logits = logits[0, context_len - 1 : -1, :]
        shift_labels = full_ids[0, context_len:]
        token_nlls = F.cross_entropy(shift_logits, shift_labels, reduction="none")
        answer_tokens = self.tokenizer.convert_ids_to_tokens(shift_labels)
        return {
            "perplexity": math.exp(nll),
            "nll": nll,
            "token_nlls": token_nlls.detach().cpu().tolist(),
            "answer_tokens": answer_tokens,
            "scored_answer_tokens": int(shift_labels.numel()),
            "answer_tokens_total": answer_tokens_total,
            "context_tokens_used": context_len,
            "context_tokens_dropped": max(0, prefix_tokens_total - context_len),
        }

    def _resolve_attention_layers(self, target_layers: tuple[int | str, ...], num_layers: int) -> dict[str, int]:
        resolved: dict[str, int] = {}
        for layer in target_layers:
            if isinstance(layer, int):
                resolved[str(layer)] = min(max(layer, 0), num_layers - 1)
            elif layer == "early":
                resolved[layer] = max(0, num_layers // 6)
            elif layer == "mid":
                resolved[layer] = max(0, num_layers // 2)
            elif layer == "late":
                resolved[layer] = max(0, num_layers - 2)
            else:
                try:
                    idx = int(layer)
                except ValueError:
                    continue
                resolved[str(layer)] = min(max(idx, 0), num_layers - 1)
        return resolved

    def extract_attention(
        self,
        messages: list[dict[str, str]],
        target_layers: tuple[int | str, ...] = ("early", "mid", "late"),
        max_tokens: int = 256,
        max_map_tokens: int = 96,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"layers": {}, "target_layers": list(target_layers)}
        context_text = self.tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = self.tokenizer(context_text, return_tensors="pt", truncation=True, max_length=max_tokens).to(self.model.device)
        seq_len = int(inputs.input_ids.shape[1])
        tokens = self.tokenizer.convert_ids_to_tokens(inputs.input_ids[0])

        try:
            num_layers = len(self.model.model.layers)
        except AttributeError:
            result["status"] = "unsupported_architecture"
            result["note"] = "Model does not expose .model.layers; attention extraction requires model-specific adaptation."
            return result
        resolved_layers = self._resolve_attention_layers(target_layers, num_layers)

        # Set layers that aren't targeted to NOT output attentions to save memory
        for i, layer in enumerate(self.model.model.layers):
            try:
                attn_module = layer.self_attn if hasattr(layer, "self_attn") else layer.self_attn
            except AttributeError:
                continue
            if hasattr(attn_module, "_orig_output_attentions"):
                attn_module.output_attentions = getattr(attn_module, "_orig_output_attentions", False)

        with torch.no_grad():
            outputs = self.model(input_ids=inputs.input_ids, output_attentions=True)

        all_attentions = getattr(outputs, "attentions", None)
        if all_attentions is None:
            result["status"] = "no_attention_returned"
            return result

        if seq_len > max_map_tokens:
            sample_positions = torch.linspace(0, seq_len - 1, steps=max_map_tokens).long()
        else:
            sample_positions = torch.arange(seq_len)

        sampled_tokens = [tokens[int(idx)] for idx in sample_positions]
        result["tokens"] = sampled_tokens
        result["sampled_positions"] = [int(idx) for idx in sample_positions]
        result["seq_len"] = seq_len

        for layer_name, layer_idx in resolved_layers.items():
            if layer_idx >= num_layers or layer_idx >= len(all_attentions):
                result["layers"][layer_name] = {"status": "out_of_range", "layer_index": layer_idx}
                continue
            attn_tensor = all_attentions[layer_idx].detach()
            mean_attention = attn_tensor[0].mean(dim=0)
            sampled_attention = mean_attention.index_select(0, sample_positions.to(mean_attention.device)).index_select(1, sample_positions.to(mean_attention.device))
            layer_result: dict[str, Any] = {
                "status": "ok",
                "layer_index": int(layer_idx),
                "num_heads": int(attn_tensor.shape[1]),
                "seq_len": seq_len,
                "mean_max_attention": float(attn_tensor.max(dim=-1).values.mean().cpu()),
                "mean_entropy": float(
                    -(attn_tensor * (attn_tensor + 1e-9).log()).sum(dim=-1).mean().cpu()
                ),
                "attention": sampled_attention.cpu().tolist(),
            }
            result["layers"][layer_name] = layer_result
            del sampled_attention
            del mean_attention
            del attn_tensor

        del all_attentions
        del outputs
        result["status"] = "ok"
        return result
