from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).parents[1]
STATE_TOOL = ROOT / "scripts" / "privileged_matrix_state.py"


def load_state(name: str):
    spec = importlib.util.spec_from_file_location(name, STATE_TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_continuation_cli_contract_is_explicit() -> None:
    state = load_state("continuation_state_cli_test")
    parser = state.parser()
    option_strings = {
        option
        for action in ("create-continuation-plan", "verify-continuation-plan")
        for option in next(
            choice
            for choice in parser._actions
            if getattr(choice, "choices", None)
        ).choices[action]._option_string_actions
    }
    assert {"--parent-root", "--qa-manifest", "--summary-manifest"} <= option_strings
    assert state.CONTINUATION_SCHEMA == (
        "commu-secure-single-matrix-continuation-plan-v1"
    )
    assert state.PARENT_LEDGER_SCHEMA == "commu-matrix-parent-ledger-v1"
    assert "revalidate_parent=False" in inspect.getsource(state.seal_cell)
    assert "revalidate_parent=True" in inspect.getsource(state.verify_matrix_marker)


def test_continuation_union_rejects_overlap_and_reused_attempts() -> None:
    state = load_state("continuation_state_union_test")
    request_ids = [f"qa-{number:03d}" for number in range(96)]
    expected = [
        {"request_id": request_id, "repetition": repetition}
        for request_id in request_ids
        for repetition in range(1, 4)
    ]
    parent_key = ("qa-000", 1)
    cell = {
        "completed": [{"request_id": parent_key[0], "repetition": parent_key[1]}],
        "attempt_counts": [
            {"request_id": "qa-001", "repetition": 1, "count": 2}
        ],
        "orphan_attempt_counts": [
            {"job_id": state._job_id("qa-002", 1), "count": 4}
        ],
    }
    ledger = {"expected_keys": {"qa": expected}, "cells": {"baseline/qa/tls13": cell}}
    rows = [
        {
            "request_id": item["request_id"],
            "repetition": item["repetition"],
            "attempt": (attempt := (
                3 if (item["request_id"], item["repetition"]) == ("qa-001", 1)
                else 5 if (item["request_id"], item["repetition"]) == ("qa-002", 1)
                else 1
            )),
            "job_id": (job_id := state._job_id(item["request_id"], item["repetition"])),
            "attempt_id": f"{job_id}-attempt-{attempt:03d}-1234abcd",
            "completed": True,
        }
        for item in expected
        if (item["request_id"], item["repetition"]) != parent_key
    ]
    assert state.validate_continuation_union(
        rows, ledger, "baseline/qa/tls13", "qa"
    ) == (1, 287)

    with pytest.raises(ValueError, match="overlaps parent"):
        overlap_job = state._job_id("qa-000", 1)
        state.validate_continuation_union(
            [*rows, {"request_id": "qa-000", "repetition": 1, "attempt": 2,
                     "job_id": overlap_job,
                     "attempt_id": f"{overlap_job}-attempt-002-1234abcd"}],
            ledger,
            "baseline/qa/tls13",
            "qa",
        )
    reused = [dict(row) for row in rows]
    next(row for row in reused if row["request_id"] == "qa-002" and row["repetition"] == 1)[
        "attempt"
    ] = 4
    with pytest.raises(ValueError, match="does not continue"):
        state.validate_continuation_union(
            reused, ledger, "baseline/qa/tls13", "qa"
        )


def test_continuation_payload_bridge_allows_only_runner_plumbing(monkeypatch: pytest.MonkeyPatch) -> None:
    state = load_state("continuation_payload_bridge_test")
    old_root = Path("/old")
    new_root = Path("/new")
    common = {"repository/traffic_experiment/traffic_measure/client.py": "1" * 64}
    allowed = {
        "repository/traffic_experiment/traffic_measure/cli.py": "2" * 64,
        "repository/traffic_experiment/traffic_measure/runner.py": "3" * 64,
    }
    inventories = {
        old_root: {**common, **allowed},
        new_root: {
            **common,
            **{key: "4" * 64 for key in allowed},
        },
    }
    monkeypatch.setattr(
        state, "measurement_payload_inventory", lambda root: inventories[root]
    )
    args = SimpleNamespace(
        parent_release_root=old_root, current_release_root=new_root
    )
    state.compare_continuation_payloads(args)
    inventories[new_root][
        "repository/traffic_experiment/traffic_measure/client.py"
    ] = "5" * 64
    with pytest.raises(ValueError, match="outside reviewed ledger plumbing"):
        state.compare_continuation_payloads(args)


def _write_manifest(path: Path, prefix: str, count: int) -> str:
    path.write_text(
        "".join(
            json.dumps({"request_id": f"{prefix}-{number:03d}"}) + "\n"
            for number in range(count)
        ),
        encoding="utf-8",
    )
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _plan_values(
    root: Path, qa_sha: str, summary_sha: str, *, gpu: int, repository: str, run_id: str
) -> list[str]:
    return [
        "--root", str(root), "--repository-sha", repository,
        "--pilot-repository-sha", "a" * 40,
        "--release-files-sha256", "b" * 64,
        "--config-sha256", "c" * 64,
        "--admission-sha256", "d" * 64,
        "--active-config-sha256", "e" * 64,
        "--qa-manifest-sha256", qa_sha,
        "--summary-manifest-sha256", summary_sha,
        "--run-id", run_id, "--gpu-index", str(gpu),
        "--gpu-uuid", f"GPU-{gpu}234", "--service-uid", "0",
        "--model", "Qwen/model", "--served-model-name", "Qwen/model",
        "--model-revision", "f" * 40,
    ]


@pytest.mark.skipif(os.name == "nt" or os.getuid() != 0, reason="root POSIX semantics required")
def test_parent_snapshot_is_separate_immutable_and_tamper_evident(tmp_path: Path) -> None:
    state = load_state("continuation_parent_snapshot_test")
    parser = state.parser()
    qa = tmp_path / "qa.jsonl"
    summary = tmp_path / "summary.jsonl"
    qa_sha = _write_manifest(qa, "qa", 96)
    summary_sha = _write_manifest(summary, "summary", 60)
    parent = tmp_path / "parent"
    target = tmp_path / "target"
    parent.mkdir()
    target.mkdir()
    parent_values = _plan_values(
        parent, qa_sha, summary_sha, gpu=3, repository="a" * 40, run_id="parent-run"
    )
    state.plan_action(parser.parse_args(["create-plan", *parent_values]))

    cell = parent / "cells/baseline/qa/tls13"
    captures = cell / "captures"
    captures.mkdir(parents=True)
    request_id, repetition = "qa-000", 1
    job_id = state._job_id(request_id, repetition)
    attempt_id = f"{job_id}-attempt-001-1234abcd"
    capture = captures / f"{attempt_id}.pcapng"
    capture.write_bytes(b"pcap")
    row = {
        "request_id": request_id, "repetition": repetition,
        "job_id": job_id, "attempt": 1, "attempt_id": attempt_id,
        "manifest_sha256": qa_sha, "worker_count": 1, "worker_index": 0,
        "worker_gpu_index": 3, "worker_gpu_uuid": "GPU-3234",
        "topology_worker_index": 0, "transport": "tls13",
        "connection_mode": "warm", "backend_port": 8443,
        "completed": True, "capture_file": str(capture),
        "capture_sha256": hashlib.sha256(b"pcap").hexdigest(),
    }
    results = cell / "results.jsonl"
    results.write_text(json.dumps(row) + "\n", encoding="utf-8")
    parent_before = {
        path: (path.read_bytes(), path.stat().st_ino)
        for path in (parent / "RUN_PLAN.json", results, capture)
    }

    target_values = _plan_values(
        target, qa_sha, summary_sha, gpu=4, repository="9" * 40,
        run_id="continuation-run",
    )
    continuation_values = [
        *target_values, "--parent-root", str(parent),
        "--qa-manifest", str(qa), "--summary-manifest", str(summary),
    ]
    mismatched_target = tmp_path / "mismatched-target"
    mismatched_target.mkdir()
    mismatched_values = _plan_values(
        mismatched_target, qa_sha, summary_sha, gpu=4, repository="9" * 40,
        run_id="mismatched-model-run",
    )
    mismatched_values[mismatched_values.index("Qwen/model")] = "Other/model"
    with pytest.raises(ValueError, match="frozen parent workload"):
        state.continuation_plan_action(
            parser.parse_args(
                ["create-continuation-plan", *mismatched_values,
                 "--parent-root", str(parent), "--qa-manifest", str(qa),
                 "--summary-manifest", str(summary)]
            )
        )
    state.continuation_plan_action(
        parser.parse_args(["create-continuation-plan", *continuation_values])
    )
    plan = json.loads((target / "RUN_PLAN.json").read_text(encoding="utf-8"))
    assert plan["source_parent_repository_sha"] == "a" * 40
    assert plan["target_orchestration_repository_sha"] == "9" * 40
    assert plan["parent_snapshot"]["gpu_uuid"] == "GPU-3234"
    assert plan["gpu_uuid"] == "GPU-4234"
    assert all(
        (path.read_bytes(), path.stat().st_ino) == value
        for path, value in parent_before.items()
    )
    state.continuation_plan_action(
        parser.parse_args(["verify-continuation-plan", *continuation_values])
    )

    results.write_text(results.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed after continuation snapshot"):
        state.continuation_plan_action(
            parser.parse_args(["verify-continuation-plan", *continuation_values])
        )
