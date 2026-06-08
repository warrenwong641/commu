from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import pandas as pd

from locomo_eval.experiments.config import ExperimentConfig
from locomo_eval.experiments.runner import ExperimentRunner
from locomo_eval.locomo.loader import load_conversations
from locomo_eval.locomo.qa_builder import ConversationPrecomputed, conversations_to_frame, precompute_retrieval, save_precomputed
from locomo_eval.models.qwen_adapter import QwenAdapter
from locomo_eval.reports.attention_examples import plot_attention_examples
from locomo_eval.reports.failure_cases import export_failure_cases
from locomo_eval.reports.heatmaps import plot_attention_heatmap
from locomo_eval.reports.perplexity import plot_token_perplexity_distribution
from locomo_eval.reports.plots import plot_budget_vs_performance
from locomo_eval.reports.tables import build_ablation_table, build_category_table

LOGGER = logging.getLogger(__name__)


def cmd_download_data(_: argparse.Namespace) -> int:
    # Try HF datasets API first
    try:
        from datasets import load_dataset
        LOGGER.info("Attempting download via datasets.load_dataset('snap-research/LoCoMo')...")
        ds = load_dataset("snap-research/LoCoMo", split="train")
        target_dir = Path("data/raw")
        target_dir.mkdir(parents=True, exist_ok=True)
        df = ds.to_pandas()
        for idx, record in df.iterrows():
            record_dict = record.to_dict()
            target_path = target_dir / f"locomo_{idx}.json"
            with target_path.open("w", encoding="utf-8") as handle:
                json.dump(record_dict, handle, ensure_ascii=False, indent=2)
        print(f"Downloaded {len(df)} conversations to {target_dir.resolve()}")
        return 0
    except Exception as exc:
        LOGGER.info("datasets API failed: %s", exc)

    # Try huggingface_hub snapshot
    try:
        from huggingface_hub import snapshot_download
        LOGGER.info("Attempting download via huggingface_hub.snapshot_download('snap-research/LoCoMo')...")
        local = snapshot_download("snap-research/LoCoMo", repo_type="dataset", local_dir="data/raw")
        print(f"Downloaded to {local}")
        return 0
    except Exception as exc:
        LOGGER.info("huggingface_hub failed: %s", exc)

    print("Automatic download failed (network / HF access may be blocked).")
    print("Manual steps:")
    print("1. Visit https://huggingface.co/datasets/snap-research/LoCoMo")
    print("2. Download all JSON files")
    print(f"3. Place them in: {Path('data/raw').resolve()}")
    print("4. Run: python -m locomo_eval.cli prepare")
    return 0


def cmd_prepare(args: argparse.Namespace) -> int:
    conversations = load_conversations(args.data_dir)
    processed_dir = Path(args.processed_dir)
    processed_dir.mkdir(parents=True, exist_ok=True)
    for conversation in conversations:
        save_precomputed(precompute_retrieval(conversation), processed_dir)
    frame = conversations_to_frame(conversations)
    frame.to_parquet(processed_dir / "qa_examples.parquet", index=False)
    stats: dict[str, int] = {"conversations": len(conversations), "qa_examples": len(frame)}
    for conversation in conversations:
        stats[f"conv_{conversation.conversation_id}_sessions"] = len(conversation.sessions)
        stats[f"conv_{conversation.conversation_id}_turns"] = len(conversation.all_turns)
    print(json.dumps(stats, indent=2))
    return 0


def cmd_pregenerate_summaries(args: argparse.Namespace) -> int:
    conversations = load_conversations(args.data_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.generate:
        adapter = QwenAdapter(args.model)
    else:
        adapter = None

    for conversation in conversations:
        summaries: dict[str, str] = {}
        for session in conversation.sessions:
            existing = session.summary or ""
            if existing and not args.force_regenerate:
                summaries[session.session_id] = existing
                continue

            if args.generate and adapter is not None:
                turns_formatted = "\n".join(f"{t.speaker}: {t.text}" for t in session.turns)
                prompt = (
                    f"Summarize the key information from this conversation session in 3-5 sentences. "
                    f"Focus on facts, events, preferences, and commitments mentioned.\n\n"
                    f"Session ({session.timestamp or 'unknown time'}):\n{turns_formatted}"
                )
                messages = [{"role": "user", "content": prompt}]
                gen = adapter.generate(messages, max_new_tokens=256)
                summaries[session.session_id] = gen.text
            else:
                summaries[session.session_id] = existing

        target = output_dir / f"{conversation.conversation_id}_summaries.json"
        with target.open("w", encoding="utf-8") as handle:
            json.dump(summaries, handle, indent=2)
        print(f"Saved {len(summaries)} session summaries to {target}")

    return 0


def cmd_run(args: argparse.Namespace) -> int:
    import os
    if args.gpu_id is not None:
        os.environ["CUDA_VISIBLE_DEVICES"] = str(args.gpu_id)
    config = ExperimentConfig.from_yaml(args.config)
    if args.output_dir:
        config.output_dir = args.output_dir
    adapter = QwenAdapter(config.model)
    conversations = load_conversations(args.data_dir)
    runner = ExperimentRunner(config, adapter)
    runner.run(
        conversations,
        dry_run=args.dry_run,
        shard_index=args.shard_index,
        num_shards=args.num_shards,
    )
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    import glob
    frames = []
    for pattern in args.input_dirs:
        paths = sorted(glob.glob(str(Path(pattern) / "results.parquet")))
        if not paths:
            paths = sorted(glob.glob(str(Path(pattern) / "*.parquet")))
            if not paths:
                print(f"Warning: no parquet files found in {pattern}")
                continue
        for p in paths:
            frames.append(pd.read_parquet(p))
    if not frames:
        print("No results to merge")
        return 1
    merged = pd.concat(frames, ignore_index=True).drop_duplicates(
        subset=["conversation_id", "question_id", "method", "budget_label"],
        keep="last",
    )
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / "results.parquet"
    merged.to_parquet(target, index=False)
    print(f"Merged {len(frames)} shards → {len(merged)} rows → {target}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    results_path = Path(args.results_dir) / (args.results_file or "results.parquet")
    if not results_path.exists():
        print(f"Results file not found: {results_path}")
        return 1
    results = pd.read_parquet(results_path)
    table = build_ablation_table(results)
    print(table.to_string(index=False))
    table.to_csv(Path(args.results_dir) / "ablation_table.csv", index=False)
    category_table = build_category_table(results)
    if not category_table.empty:
        print(category_table.to_string(index=False))
        category_table.to_csv(Path(args.results_dir) / "category_table.csv", index=False)
    plot_budget_vs_performance(results, Path(args.results_dir) / "plots")
    plot_attention_heatmap(results, Path(args.results_dir) / "plots")
    plot_attention_examples(results, Path(args.results_dir) / "plots")
    plot_token_perplexity_distribution(results, Path(args.results_dir) / "plots")
    export_failure_cases(results, args.results_dir)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    download_parser = subparsers.add_parser("download-data")
    download_parser.set_defaults(func=cmd_download_data)

    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--data-dir", default="data/raw")
    prepare_parser.add_argument("--processed-dir", default="data/processed")
    prepare_parser.set_defaults(func=cmd_prepare)

    summary_parser = subparsers.add_parser("pregenerate-summaries")
    summary_parser.add_argument("--data-dir", default="data/raw")
    summary_parser.add_argument("--output-dir", default="data/processed")
    summary_parser.add_argument("--model", default="Qwen/Qwen3-4B-Instruct-2507")
    summary_parser.add_argument("--generate", action="store_true", help="Use model to generate summaries (expensive)")
    summary_parser.add_argument("--force-regenerate", action="store_true")
    summary_parser.set_defaults(func=cmd_pregenerate_summaries)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--config", default="configs/first_experiment.yaml")
    run_parser.add_argument("--data-dir", default="data/raw")
    run_parser.add_argument("--dry-run", action="store_true")
    run_parser.add_argument("--gpu-id", type=int, default=None)
    run_parser.add_argument("--num-shards", type=int, default=1)
    run_parser.add_argument("--shard-index", type=int, default=0)
    run_parser.add_argument("--output-dir", default=None)
    run_parser.set_defaults(func=cmd_run)

    merge_parser = subparsers.add_parser("merge")
    merge_parser.add_argument("--input-dirs", nargs="+", required=True)
    merge_parser.add_argument("--output-dir", required=True)
    merge_parser.set_defaults(func=cmd_merge)

    report_parser = subparsers.add_parser("report")
    report_parser.add_argument("--results-dir", required=True)
    report_parser.add_argument("--results-file", default="results.parquet")
    report_parser.set_defaults(func=cmd_report)
    return parser


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
