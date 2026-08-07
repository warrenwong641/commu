from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import tempfile

from .analyze import analyze_results
from .common import append_jsonl, read_jsonl, sha256_file
from .prepare import (
    DEFAULT_SYSTEM_PROMPT,
    SUMMARY_SYSTEM_PROMPT,
    SUPPORTED_CONDITIONS,
    merge_manifest_shards,
    prepare_manifest,
    prepare_summary_manifest,
)
from .report import generate_report
from .runner import RunSettings, health_check, run_experiment
from .session_timeline import analyze_session_timeline
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
    run.add_argument(
        "--capture-stop-on-response",
        action="store_true",
        help=(
            "stop and flush capture when the response completes; observation-seconds "
            "then acts as a safety ceiling"
        ),
    )
    run.add_argument("--worker-count", type=int, default=1)
    run.add_argument("--worker-index", type=int, default=0)
    run.add_argument("--no-capture", action="store_true")
    run.add_argument("--no-wait-after-request", action="store_true", help=argparse.SUPPRESS)
    run.add_argument("--session-id")
    run.add_argument(
        "--condition",
        choices=sorted(SUPPORTED_CONDITIONS),
        help="run only one frozen compression condition",
    )
    run.add_argument(
        "--inter-request-delay-seconds",
        type=float,
        default=0,
        help="closed-loop pause after a completed response before the next request",
    )
    run.add_argument(
        "--request-start-interval-seconds",
        type=float,
        default=0,
        help=(
            "target interval between consecutive request starts; if a response "
            "runs longer, the next request starts only after it completes"
        ),
    )
    run.add_argument(
        "--session-budget-seconds",
        type=float,
        default=0,
        help=(
            "soft admission window: finish an in-flight response, but do not "
            "start another request after this many session seconds"
        ),
    )

    analyze = subparsers.add_parser("analyze", help="extract traffic metrics with tshark")
    analyze.add_argument("--results", type=Path, required=True)
    analyze.add_argument("--output", type=Path, required=True)
    analyze.add_argument("--tshark", default="tshark")

    timeline = subparsers.add_parser(
        "session-timeline",
        help="index a complete warm session into fixed time segments and prompt events",
    )
    timeline.add_argument("--results", type=Path, required=True)
    timeline.add_argument("--capture", type=Path, required=True)
    timeline.add_argument("--output-dir", type=Path, required=True)
    timeline.add_argument("--server-port", type=int, required=True)
    timeline.add_argument("--transport", choices=["tls13", "http3"], required=True)
    timeline.add_argument("--segment-seconds", type=float, default=30)
    timeline.add_argument("--tshark", default="tshark")

    report = subparsers.add_parser(
        "report",
        help="generate comprehensive CSV tables and figure data from completed runs",
    )
    report.add_argument("--results", type=Path, required=True,
                        help="merged results.jsonl from a completed experiment")
    report.add_argument("--output-dir", type=Path, required=True,
                        help="directory for output CSV files")
    report.add_argument("--seed", type=int, default=42)
    report.add_argument("--tshark", default="tshark")

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

    repair = subparsers.add_parser(
        "repair",
        help="bind an existing external PCAP to a completed results row without traffic",
    )
    repair.add_argument("--results", type=Path, required=True,
                        help="existing results.jsonl to update in-place")
    repair.add_argument("--pcap", type=Path, required=True,
                        help="existing PCAP file to attach")
    repair.add_argument("--request-id", required=True,
                        help="request_id of the row to repair")
    repair.add_argument("--condition", default="no_compression",
                        help="compression condition of the row")
    repair.add_argument("--transport", choices=["tls13", "http3"], required=True,
                        help="transport protocol of the row")
    repair.add_argument("--capture-interface", required=True,
                        help="interface used by the external capture")
    repair.add_argument("--capture-filter", required=True,
                        help="BPF applied by the external capture")
    repair.add_argument("--manifest-sha256", required=True,
                        help="SHA-256 of the frozen manifest used for the row")
    repair.add_argument("--measurement-config-sha256", required=True,
                        help="SHA-256 of the fixed measurement definition")
    return parser


def _repair_results(
    results_path: Path,
    pcap_path: Path,
    request_id: str,
    condition: str,
    transport: str,
    capture_interface: str,
    capture_filter: str,
    manifest_sha256: str,
    measurement_config_sha256: str,
) -> int:
    """Attach an existing external PCAP to a completed results row.

    Reads *results_path*, finds the matching row, computes the PCAP
    SHA-256, updates capture_file + capture_sha256 in-place with an
    append-only audit trail, and writes back atomically.  No traffic is
    sent.  The row must already be completed=True.
    """
    if not pcap_path.is_file() or pcap_path.stat().st_size <= 0:
        print(
            f"ERROR: PCAP is missing or empty: {pcap_path}",
            file=__import__("sys").stderr,
        )
        return 1
    for label, digest in (
        ("manifest", manifest_sha256),
        ("measurement config", measurement_config_sha256),
    ):
        if len(digest) != 64 or any(
            character not in "0123456789abcdefABCDEF"
            for character in digest
        ):
            print(
                f"ERROR: {label} SHA-256 is malformed.",
                file=__import__("sys").stderr,
            )
            return 1
    rows = read_jsonl(results_path)
    target_idx = None
    for i, row in enumerate(rows):
        if (str(row.get("request_id")) == request_id
                and str(row.get("condition")) == condition
                and str(row.get("transport")) == transport):
            target_idx = i
            break
    if target_idx is None:
        print(f"ERROR: no row matching request_id={request_id} "
              f"condition={condition} transport={transport}", file=__import__("sys").stderr)
        return 1
    row = rows[target_idx]
    if not row.get("completed"):
        print("ERROR: row is not completed — refusing to attach PCAP", file=__import__("sys").stderr)
        return 1
    if row.get("capture_file") and row.get("capture_sha256"):
        print(f"Row already has capture_file={row['capture_file']} — skipping", file=__import__("sys").stderr)
        return 0

    cap_hash = sha256_file(pcap_path)
    row["capture_file"] = str(pcap_path)
    row["capture_sha256"] = cap_hash
    row["capture_interface"] = capture_interface
    row["capture_filter"] = capture_filter
    row["capture_return_code"] = 0
    row["capture_stderr"] = None
    row["capture_may_be_truncated"] = False
    row["capture_stop_on_response"] = True
    row["external_capture"] = True
    row["manifest_sha256"] = manifest_sha256
    row["measurement_config_sha256"] = measurement_config_sha256
    row["repair_attached_pcap"] = True
    row["repair_timestamp_utc"] = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    ).isoformat()

    # Atomic write: temp file + rename.
    tmp = results_path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(__import__("json").dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    tmp.replace(results_path)

    # Append audit entry.
    audit = results_path.parent / "repair_audit.jsonl"
    append_jsonl(audit, {
        "action": "repair_attach_pcap",
        "results_path": str(results_path),
        "pcap_path": str(pcap_path),
        "pcap_sha256": cap_hash,
        "request_id": request_id,
        "condition": condition,
        "transport": transport,
        "capture_interface": capture_interface,
        "capture_filter": capture_filter,
        "manifest_sha256": manifest_sha256,
        "measurement_config_sha256": measurement_config_sha256,
        "timestamp_utc": row["repair_timestamp_utc"],
    })
    print(f"Repaired: {request_id} → capture_file={pcap_path} sha256={cap_hash}")
    return 0


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
        models = health_check(
            args.base_url,
            os.environ.get("LOCAL_VLLM_API_KEY", ""),
        )
        print(json.dumps(models, indent=2, ensure_ascii=False))
        return 0

    if args.command == "run":
        base_url = args.base_url or {
            "local_vllm": "http://127.0.0.1:8000/v1",
            "openrouter": "https://openrouter.ai/api/v1",
            "gemini": "https://generativelanguage.googleapis.com/v1beta",
        }[args.backend]
        api_key = {
            "local_vllm": os.environ.get("LOCAL_VLLM_API_KEY", ""),
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
                capture_stop_on_response=args.capture_stop_on_response,
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
                request_start_interval_seconds=args.request_start_interval_seconds,
                session_budget_seconds=args.session_budget_seconds,
                condition=args.condition,
            )
        )
        print(f"Results: {results}")
        return 0

    if args.command == "analyze":
        output = analyze_results(args.results, args.output, tshark=args.tshark)
        print(f"Analysis: {output}")
        return 0
    if args.command == "session-timeline":
        segments, prompts = analyze_session_timeline(
            results_path=args.results,
            capture_path=args.capture,
            output_dir=args.output_dir,
            server_port=args.server_port,
            transport=args.transport,
            segment_seconds=args.segment_seconds,
            tshark=args.tshark,
        )
        print(f"Session segments: {segments}")
        print(f"Prompt upload events: {prompts}")
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
    if args.command == "report":
        outputs = generate_report(
            args.results,
            args.output_dir,
            seed=args.seed,
            tshark=args.tshark,
        )
        for name, path in sorted(outputs.items()):
            print(f"{name}: {path}")
        return 0
    if args.command == "repair":
        return _repair_results(
            results_path=args.results,
            pcap_path=args.pcap,
            request_id=args.request_id,
            condition=args.condition,
            transport=args.transport,
            capture_interface=args.capture_interface,
            capture_filter=args.capture_filter,
            manifest_sha256=args.manifest_sha256,
            measurement_config_sha256=args.measurement_config_sha256,
        )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
