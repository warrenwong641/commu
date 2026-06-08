from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from locomo_eval.experiments.config import ExperimentConfig
from locomo_eval.experiments.runner import ExperimentRunner
from locomo_eval.locomo.loader import load_conversations
from locomo_eval.locomo.qa_builder import conversations_to_frame, precompute_retrieval, save_precomputed
from locomo_eval.models.qwen_adapter import QwenAdapter
from locomo_eval.reports.failure_cases import export_failure_cases
from locomo_eval.reports.heatmaps import plot_attention_heatmap
from locomo_eval.reports.plots import plot_budget_vs_performance
from locomo_eval.reports.tables import build_ablation_table


def cmd_download_data(_: argparse.Namespace) -> int:
    print("Automatic download is not implemented offline in this scaffold.")
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
    print(f"conversations={len(conversations)} qa_examples={len(frame)}")
    return 0


def cmd_pregenerate_summaries(_: argparse.Namespace) -> int:
    print("Pregenerate summaries is intentionally left as a model-backed offline step.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    config = ExperimentConfig.from_yaml(args.config)
    adapter = QwenAdapter(config.model)
    conversations = load_conversations(args.data_dir)
    runner = ExperimentRunner(config, adapter)
    runner.run(conversations, dry_run=args.dry_run)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    results = pd.read_parquet(Path(args.results_dir) / "results.parquet")
    table = build_ablation_table(results)
    print(table.to_string(index=False))
    plot_budget_vs_performance(results, Path(args.results_dir) / "plots")
    plot_attention_heatmap(results, Path(args.results_dir) / "plots")
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
    summary_parser.set_defaults(func=cmd_pregenerate_summaries)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--config", default="configs/first_experiment.yaml")
    run_parser.add_argument("--data-dir", default="data/raw")
    run_parser.add_argument("--dry-run", action="store_true")
    run_parser.set_defaults(func=cmd_run)

    report_parser = subparsers.add_parser("report")
    report_parser.add_argument("--results-dir", required=True)
    report_parser.set_defaults(func=cmd_report)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
