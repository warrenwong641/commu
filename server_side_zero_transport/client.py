#!/usr/bin/env python3
"""One-shot server-local web-app simulator."""
import argparse
import json
import os
import time
import urllib.request


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--prompt", required=True)
    p.add_argument("--max-tokens", type=int, default=8)
    p.add_argument("--output", required=True)
    a = p.parse_args()
    body = json.dumps({
        "model": a.model,
        "messages": [{"role": "user", "content": a.prompt}],
        "max_tokens": a.max_tokens,
        "stream": False,
    }).encode()
    started = time.time_ns()
    api_key = os.environ.get("VLLM_API_KEY")
    if not api_key:
        raise SystemExit("VLLM_API_KEY must be exported in memory")
    request = urllib.request.Request(
        a.url.rstrip("/") + "/v1/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        payload = response.read()
        status = response.status
    ended = time.time_ns()
    with open(a.output, "w", encoding="utf-8") as f:
        json.dump({"status": status, "started_ns": started, "ended_ns": ended,
                   "elapsed_ms": (ended-started)/1e6,
                   "response": json.loads(payload)}, f, indent=2)


if __name__ == "__main__":
    main()
