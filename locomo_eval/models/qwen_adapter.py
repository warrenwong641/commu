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
        self.model = AutoModelForCausalLM.from_pretrained(
            model_name,
            trust_remote_code=trust_remote_code,
            device_map="auto",
            dtype="auto",
        )
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

    def compute_perplexity(self, messages: list[dict[str, str]], answer_text: str, max_context_tokens: int = 4096) -> dict[str, Any]:
        context_text = self.tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        answer_prefix = "<|im_start|>assistant\n"
        full_text = context_text + answer_prefix + answer_text

        encoded = self.tokenizer(full_text, return_tensors="pt", truncation=True, max_length=max_context_tokens)
        full_ids = encoded.input_ids.to(self.model.device)

        context_encoded = self.tokenizer(context_text + answer_prefix, return_tensors="pt", truncation=True, max_length=max_context_tokens)
        context_len = min(context_encoded.input_ids.shape[1], full_ids.shape[1])

        labels = full_ids.clone()
        labels[:, :context_len] = -100

        with torch.no_grad():
            outputs = self.model(input_ids=full_ids, labels=labels)
        nll = float(outputs.loss.item())
        logits = outputs.logits

        if context_len >= full_ids.shape[1]:
            return {"perplexity": math.exp(nll) if nll > 0 else 1.0, "nll": nll, "token_nlls": [], "answer_tokens": []}

        shift_logits = logits[0, context_len - 1 : -1, :]
        shift_labels = full_ids[0, context_len:]
        token_nlls = F.cross_entropy(shift_logits, shift_labels, reduction="none")
        answer_tokens = self.tokenizer.convert_ids_to_tokens(shift_labels)
        return {
            "perplexity": math.exp(nll),
            "nll": nll,
            "token_nlls": token_nlls.detach().cpu().tolist(),
            "answer_tokens": answer_tokens,
        }

    def extract_attention(self, messages: list[dict[str, str]], target_layers: tuple[int, ...] = (4, 18, 32)) -> dict[str, Any]:
        result: dict[str, Any] = {"layers": {}, "target_layers": list(target_layers)}
        context_text = self.tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = self.tokenizer(context_text, return_tensors="pt", truncation=True, max_length=2048).to(self.model.device)
        seq_len = int(inputs.input_ids.shape[1])

        try:
            num_layers = len(self.model.model.layers)
        except AttributeError:
            result["status"] = "unsupported_architecture"
            result["note"] = "Model does not expose .model.layers; attention extraction requires model-specific adaptation."
            return result

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

        for layer_idx in target_layers:
            if layer_idx >= num_layers or layer_idx >= len(all_attentions):
                result["layers"][layer_idx] = {"status": "out_of_range"}
                continue
            attn_tensor = all_attentions[layer_idx].detach()
            layer_result: dict[str, Any] = {
                "status": "ok",
                "num_heads": int(attn_tensor.shape[1]),
                "seq_len": seq_len,
                "mean_max_attention": float(attn_tensor.max(dim=-1).values.mean().cpu()),
                "mean_entropy": float(
                    -(attn_tensor * (attn_tensor + 1e-9).log()).sum(dim=-1).mean().cpu()
                ),
            }
            result["layers"][layer_idx] = layer_result
            del attn_tensor

        del all_attentions
        del outputs
        result["status"] = "ok"
        return result
