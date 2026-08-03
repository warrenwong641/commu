from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote


@dataclass(frozen=True)
class BackendRequest:
    endpoint: str
    headers: dict[str, str]
    payload: dict[str, Any]


@dataclass(frozen=True)
class ParsedResponse:
    text: str
    usage: dict[str, Any] | None
    response_id: str | None
    provider: str | None = None
    model_version: str | None = None


def _sse_payloads(lines: Iterable[str]) -> Iterable[dict[str, Any]]:
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith(":") or not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        yield json.loads(payload)


def parse_openai_sse(lines: Iterable[str]) -> ParsedResponse:
    pieces: list[str] = []
    usage: dict[str, Any] | None = None
    response_id: str | None = None
    provider: str | None = None
    model_version: str | None = None
    for event in _sse_payloads(lines):
        response_id = response_id or event.get("id")
        provider = provider or event.get("provider")
        model_version = model_version or event.get("model")
        if event.get("usage"):
            usage = event["usage"]
        for choice in event.get("choices", []):
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if content:
                pieces.append(str(content))
    return ParsedResponse("".join(pieces), usage, response_id, provider, model_version)


def parse_gemini_sse(lines: Iterable[str]) -> ParsedResponse:
    pieces: list[str] = []
    usage: dict[str, Any] | None = None
    response_id: str | None = None
    model_version: str | None = None
    for event in _sse_payloads(lines):
        response_id = response_id or event.get("responseId")
        model_version = model_version or event.get("modelVersion")
        if event.get("usageMetadata"):
            usage = event["usageMetadata"]
        for candidate in event.get("candidates", []):
            content = candidate.get("content") or {}
            for part in content.get("parts", []):
                if "text" in part:
                    pieces.append(str(part["text"]))
    return ParsedResponse("".join(pieces), usage, response_id, "google", model_version)


def build_backend_request(
    *,
    backend: str,
    base_url: str,
    model: str,
    api_key: str,
    messages: list[dict[str, str]],
    generation: dict[str, Any],
    openrouter_provider: str | None = None,
) -> BackendRequest:
    if backend in {"local_vllm", "openrouter"}:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            **generation,
        }
        if backend == "openrouter":
            if not openrouter_provider:
                raise ValueError("OpenRouter requires --openrouter-provider for pinned routing")
            payload["provider"] = {
                "only": [openrouter_provider],
                "allow_fallbacks": False,
            }
        return BackendRequest(
            endpoint=base_url.rstrip("/") + "/chat/completions",
            headers=headers,
            payload=payload,
        )

    if backend == "gemini":
        if not api_key:
            raise ValueError("Gemini requires an API key")
        system_parts = [
            {"text": message["content"]}
            for message in messages
            if message.get("role") == "system"
        ]
        contents = [
            {
                "role": "model" if message.get("role") == "assistant" else "user",
                "parts": [{"text": message["content"]}],
            }
            for message in messages
            if message.get("role") != "system"
        ]
        payload = {
            "contents": contents,
            "generationConfig": {
                "temperature": generation["temperature"],
                "maxOutputTokens": generation["max_tokens"],
            },
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": system_parts}
        endpoint = (
            f"{base_url.rstrip('/')}/models/{quote(model, safe='')}:streamGenerateContent"
            "?alt=sse"
        )
        return BackendRequest(
            endpoint=endpoint,
            headers={"x-goog-api-key": api_key},
            payload=payload,
        )

    raise ValueError(f"unsupported backend: {backend}")


def parse_backend_response(backend: str, lines: Iterable[str]) -> ParsedResponse:
    if backend == "gemini":
        return parse_gemini_sse(lines)
    return parse_openai_sse(lines)


def normalized_usage(backend: str, usage: dict[str, Any] | None) -> tuple[int | None, int | None]:
    if not usage:
        return None, None
    if backend == "gemini":
        return usage.get("promptTokenCount"), usage.get("candidatesTokenCount")
    return usage.get("prompt_tokens"), usage.get("completion_tokens")
