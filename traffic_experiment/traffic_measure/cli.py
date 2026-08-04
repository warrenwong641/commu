from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .analyze import analyze_results
from .prepare import (
    DEFAULT_SYSTEM_PROMPT,
    SUMMARY_SYSTEM_PROMPT,
    SUPPORTED_CONDITIONS,
    merge_manifest_shards,
    prepare_manifest,
    prepare_summary_manifest,
)
from .runner import RunSettings, health_check, run_experiment
from .vllm_metrics import (
    diff_vllm_snapshots,
    fetch_vllm_snapshot,
    read_json,
    write_json,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local vLLM traffic measurement tools")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="freeze LoCoMo prompts and precompute compression")
    prepare.add_argument("--data-dir", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--samples", type=int, default=32)
    prepare.add_argument("--seed", type=int, default=42)
    prepare.add_argument(
        "--conditions",
        nargs="+",
        choices=sorted(SUPPORTED_CONDITIONS),
        default=list(SUPPORTED_CONDITIONS),
    )
    prepare.add_argument("--compressor-model", default="NousResearch/Llama-2-7b-hf")
    prepare.add_argument("--compressor-device", default="cuda")
    prepare.add_argument("--system-prompt", default=DEFAULT_SYSTEM_PROMPT)
    prepare.add_argument("--shard-count", type=int, default=1)
    prepare.add_argument("--shard-index", type=int, default=0)

    prepare_summary = subparsers.add_parser(
        "prepare-summaries", help="freeze LoCoMo event-summary prompts and references"
    )
    prepare_summary.add_argument("--data-dir", type=Path, required=True)
    prepare_summary.add_argument("--output", type=Path, required=True)
    prepare_summary.add_argument("--conversations", type=int, default=10)
    prepare_summary.add_argument("--seed", type=int, default=42)
    prepare_summary.add_argument(
        "--conditions",
        nargs="+",
        choices=sorted(SUPPORTED_CONDITIONS),
        default=list(SUPPORTED_CONDITIONS),
    )
    prepare_summary.add_argument("--compressor-model", default="NousResearch/Llama-2-7b-hf")
    prepare_summary.add_argument("--compressor-device", default="cuda")
    prepare_summary.add_argument("--system-prompt", default=SUMMARY_SYSTEM_PROMPT)

    merge = subparsers.add_parser("merge-manifests", help="merge deterministic preparation shards")
    merge.add_argument("--input", type=Path, nargs="+", required=True)
    merge.add_argument("--output", type=Path, required=True)
    merge.add_argument("--expected-rows", type=int)

    check = subparsers.add_parser("check", help="verify the local vLLM endpoint")
    check.add_argument("--base-url", default="http://127.0.0.1:8000/v1")
    check.add_argument("--api-key", default=os.environ.get("LOCAL_VLLM_API_KEY", "local-test-key"))

    run = subparsers.add_parser("run", help="run measured requests")
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--output-dir", type=Path, required=True)
    run.add_argument("--base-url")
    run.add_argument("--model", default="Qwen/Qwen3-8B")
    run.add_argument(
        "--backend",
        choices=["local_vllm", "openrouter", "gemini"],
        default="local_vllm",
    )
    run.add_argument("--api-key")
    run.add_argument("--openrouter-provider")
    run.add_argument("--transport", choices=["http1", "tls13", "http3"], default="http1")
    run.add_argument("--connection-mode", choices=["warm", "cold"], default="warm")
    run.add_argument("--tls-ca-file", type=Path)
    run.add_argument("--curl-executable", default="curl")
    run.add_argument("--samples", type=int, required=True)
    run.add_argument("--repetitions", type=int, required=True)
    run.add_argument("--seed", type=int, default=42)
    run.add_argument("--temperature", type=float, default=0)
    run.add_argument("--max-output-tokens", type=int, default=256)
    run.add_argument("--request-timeout-seconds", type=float, default=120)
    run.add_argument("--observation-seconds", type=int, default=30)
    run.add_argument("--capture-interface", default="")
    run.add_argument("--capture-filter", default="tcp port 8000")
    run.add_argument("--capture-startup-delay-seconds", type=float, default=0.5)
    run.add_argument("--worker-count", type=int, default=1)
    run.add_argument("--worker-index", type=int, default=0)
    run.add_argument("--no-capture", action="store_true")
    run.add_argument("--no-wait-after-request", action="store_true", help=argparse.SUPPRESS)
    run.add_argument("--session-id")
    run.add_argument(
        "--inter-request-delay-seconds",
        type=float,
        default=0,
        help="closed-loop pause after a completed response before the next request",
    )

    analyze = subparsers.add_parser("analyze", help="extract traffic metrics with tshark")
    analyze.add_argument("--results", type=Path, required=True)
    analyze.add_argument("--output", type=Path, required=True)
    analyze.add_argument("--tshark", default="tshark")

    snapshot = subparsers.add_parser(
        "server-snapshot",
        help="capture token, request, queue, and latency counters from vLLM",
    )
    snapshot.add_argument("--metrics-url", default="http://127.0.0.1:8000/metrics")
    snapshot.add_argument("--output", type=Path, required=True)
    snapshot.add_argument("--timeout-seconds", type=float, default=10)

    diff = subparsers.add_parser(
        "server-diff",
        help="calculate vLLM counter deltas and rates between two snapshots",
    )
    diff.add_argument("--before", type=Path, required=True)
    diff.add_argument("--after", type=Path, required=True)
    diff.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "prepare":
        rows = prepare_manifest(
            data_dir=args.data_dir,
            output_path=args.output,
            sample_count=args.samples,
            seed=args.seed,
            conditions=args.conditions,
            compressor_model=args.compressor_model,
            compressor_device=args.compressor_device,
            system_prompt=args.system_prompt,
            shard_count=args.shard_count,
            shard_index=args.shard_index,
        )
        print(f"Wrote {len(rows)} condition rows to {args.output}")
        return 0

    if args.command == "prepare-summaries":
        rows = prepare_summary_manifest(
            data_dir=args.data_dir,
            output_path=args.output,
            conversation_count=args.conversations,
            seed=args.seed,
            conditions=args.conditions,
            compressor_model=args.compressor_model,
            compressor_device=args.compressor_device,
            system_prompt=args.system_prompt,
        )
        print(f"Wrote {len(rows)} event-summary condition rows to {args.output}")
        return 0

    if args.command == "merge-manifests":
        rows = merge_manifest_shards(
            input_paths=args.input,
            output_path=args.output,
            expected_rows=args.expected_rows,
        )
        print(f"Merged {len(rows)} condition rows into {args.output}")
        return 0

    if args.command == "check":
        models = health_check(args.base_url, args.api_key)
        print(json.dumps(models, indent=2, ensure_ascii=False))
        return 0

    if args.command == "run":
        base_url = args.base_url or {
            "local_vllm": "http://127.0.0.1:8000/v1",
            "openrouter": "https://openrouter.ai/api/v1",
            "gemini": "https://generativelanguage.googleapis.com/v1beta",
        }[args.backend]
        api_key = args.api_key
        if api_key is None:
            api_key = {
                "local_vllm": os.environ.get("LOCAL_VLLM_API_KEY", "local-test-key"),
                "openrouter": os.environ.get("OPENROUTER_API_KEY", ""),
                "gemini": os.environ.get("GEMINI_API_KEY", ""),
            }[args.backend]
        results = run_experiment(
            RunSettings(
                manifest_path=args.manifest,
                output_dir=args.output_dir,
                base_url=base_url,
                model=args.model,
                api_key=api_key,
                sample_limit=args.samples,
                repetitions=args.repetitions,
                seed=args.seed,
                temperature=args.temperature,
                max_output_tokens=args.max_output_tokens,
                request_timeout_seconds=args.request_timeout_seconds,
                observation_seconds=args.observation_seconds,
                capture_interface=args.capture_interface,
                capture_filter=args.capture_filter,
                capture_startup_delay_seconds=args.capture_startup_delay_seconds,
                worker_count=args.worker_count,
                worker_index=args.worker_index,
                no_capture=args.no_capture,
                no_wait_after_request=args.no_wait_after_request,
                backend=args.backend,
                transport=args.transport,
                connection_mode=args.connection_mode,
                openrouter_provider=args.openrouter_provider,
                tls_ca_file=args.tls_ca_file,
                curl_executable=args.curl_executable,
                session_id=args.session_id,
                inter_request_delay_seconds=args.inter_request_delay_seconds,
            )
        )
        print(f"Results: {results}")
        return 0

    if args.command == "analyze":
        output = analyze_results(args.results, args.output, tshark=args.tshark)
        print(f"Analysis: {output}")
        return 0
    if args.command == "server-snapshot":
        snapshot = fetch_vllm_snapshot(
            args.metrics_url,
            timeout_seconds=args.timeout_seconds,
        )
        write_json(args.output, snapshot)
        print(f"Server metrics snapshot: {args.output}")
        return 0
    if args.command == "server-diff":
        diff = diff_vllm_snapshots(
            read_json(args.before),
            read_json(args.after),
        )
        write_json(args.output, diff)
        print(f"Server metrics difference: {args.output}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
