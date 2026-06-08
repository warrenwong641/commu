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
            torch_dtype="auto",
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
                temperature=0.0,
                top_p=0.8,
                top_k=20,
            )
        generated_ids = outputs[0][inputs.input_ids.shape[1]:]
        generated_text = self.tokenizer.decode(generated_ids, skip_special_tokens=True)
        return GenerationResult(
            text=generated_text.strip(),
            prompt_tokens=int(inputs.input_ids.shape[1]),
            completion_tokens=int(generated_ids.shape[0]),
            metadata={},
        )

    def compute_perplexity(self, messages: list[dict[str, str]], answer_text: str) -> dict[str, Any]:
        context_text = self.tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        answer_prefix = "<|im_start|>assistant\n"
        context_ids = self.tokenizer(context_text + answer_prefix, return_tensors="pt").input_ids.to(self.model.device)
        full_ids = self.tokenizer(context_text + answer_prefix + answer_text, return_tensors="pt").input_ids.to(self.model.device)
        labels = full_ids.clone()
        labels[:, : context_ids.shape[1]] = -100
        with torch.no_grad():
            outputs = self.model(input_ids=full_ids, labels=labels)
        nll = float(outputs.loss.item())
        logits = outputs.logits
        shift_logits = logits[0, context_ids.shape[1] - 1 : -1, :]
        shift_labels = full_ids[0, context_ids.shape[1] :]
        token_nlls = F.cross_entropy(shift_logits, shift_labels, reduction="none")
        answer_tokens = self.tokenizer.convert_ids_to_tokens(shift_labels)
        return {
            "perplexity": math.exp(nll),
            "nll": nll,
            "token_nlls": token_nlls.detach().cpu().tolist(),
            "answer_tokens": answer_tokens,
        }

    def extract_attention(self, messages: list[dict[str, str]], target_layers: tuple[int, ...] = (4, 18, 32)) -> dict[str, Any]:
        return {
            "status": "not_verified",
            "target_layers": list(target_layers),
            "note": "Attention extraction requires runtime verification against the loaded Qwen implementation.",
        }
