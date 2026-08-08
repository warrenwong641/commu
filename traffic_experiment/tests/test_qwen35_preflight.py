from __future__ import annotations

import ast
import importlib.metadata
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1]
ENV_DIR = ROOT / "environments" / "qwen35"
MODULE_SPEC = importlib.util.spec_from_file_location(
    "qwen35_preflight", ENV_DIR / "preflight.py"
)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
preflight = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = preflight
MODULE_SPEC.loader.exec_module(preflight)
PINNED = json.loads((ENV_DIR / "spec.json").read_text(encoding="utf-8"))


def good_config() -> dict[str, object]:
    return {
        "_commit_hash": PINNED["model"]["revision"],
        "architectures": ["Qwen3_5ForConditionalGeneration"],
        "model_type": "qwen3_5",
        "text_config": {"model_type": "qwen3_5_text"},
    }


def canonical_config_path(tmp_path: Path) -> Path:
    path = (
        tmp_path
        / "models--Qwen--Qwen3.5-9B"
        / "snapshots"
        / PINNED["model"]["revision"]
        / "config.json"
    )
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(good_config()), encoding="utf-8")
    return path


def test_pins_match_the_reviewed_stack():
    assert PINNED["packages"] == {
        "vllm": "0.26.0",
        "torch": "2.11.0",
        "transformers": "5.5.3",
        "tokenizers": "0.22.2",
    }
    assert PINNED["cuda"]["selected_wheel_runtime"] == "12.9"
    assert PINNED["python"] == {"major": 3, "minor": 12, "patch": 13}
    assert PINNED["model"]["revision"] == "c202236235762e1c871ad0ccb60c8ee5ba337b9a"


def test_python_check_requires_exact_patch():
    assert preflight.check_python(PINNED, (3, 12, 13))[0].level == "ok"
    mismatch = preflight.check_python(PINNED, (3, 12, 12))[0]
    assert mismatch.level == "error"
    assert "3.12.13" in mismatch.message


def test_lock_compiler_is_hermetic_and_fully_pinned():
    source = (ENV_DIR / "compile_requirements.sh").read_text(encoding="utf-8")
    for required in (
        'UV_VERSION="0.11.33"',
        'PYTHON_VERSION="3.12.13"',
        'PYTHON_PLATFORM="x86_64-manylinux_2_28"',
        'PYPI_INDEX="https://pypi.org/simple"',
        'EXCLUDE_NEWER="2026-08-08T12:00:00Z"',
        'UV_BIN="${UV_BIN:-uv}"',
        '"${UV_BIN}" --version',
        '"${UV_BIN}" --no-config pip compile',
        "uv --no-config pip compile",
        "--generate-hashes",
        "unset UV_CONFIG_FILE UV_DEFAULT_INDEX UV_INDEX UV_INDEX_URL "
        "UV_EXTRA_INDEX_URL",
        "unset PIP_CONFIG_FILE PIP_INDEX_URL PIP_EXTRA_INDEX_URL PIP_FIND_LINKS",
        "unset PIP_TRUSTED_HOST PYTHONPATH PYTHONHOME VIRTUAL_ENV CONDA_PREFIX",
    ):
        assert required in source


def test_lab_evidence_keeps_observations_distinct_from_live_validation():
    evidence = (ENV_DIR / "LAB_EVIDENCE.md").read_text(encoding="utf-8")
    for required in (
        "Ubuntu 24.04.2",
        "glibc 2.39",
        "NVIDIA 580.82.07",
        "compute capability 8.9",
        "49,140 MiB",
        "GPUs 2 and 3 are free but remain prohibited",
        "GPUs 4-7 host unrelated vLLM workloads and must never be touched",
        "uv 0.12.1",
        "Python is 3.12.3",
        "live runtime fitness",
    ):
        assert required in evidence


def test_recorded_snapshot_layout_uses_real_shard_names_without_code_reads():
    evidence = (ENV_DIR / "LAB_EVIDENCE.md").read_text(encoding="utf-8")
    for shard in range(1, 5):
        assert f"model.safetensors-{shard:05d}-of-00004" in evidence

    source = (ENV_DIR / "preflight.py").read_text(encoding="utf-8")
    assert "model.safetensors" not in source
    assert "read_bytes(" not in source


def test_package_check_accepts_cuda_local_version_label():
    versions = {**PINNED["packages"], "torch": "2.11.0+cu129"}
    findings = preflight.check_packages(PINNED, version_getter=versions.__getitem__)
    assert {item.level for item in findings} == {"ok"}


def test_package_check_reports_missing_and_mismatched_packages():
    def version_getter(name: str) -> str:
        if name == "vllm":
            raise importlib.metadata.PackageNotFoundError(name)
        return "0.0.0"

    findings = preflight.check_packages(PINNED, version_getter=version_getter)
    assert all(item.level == "error" for item in findings)
    assert "not installed" in findings[0].message


def test_lock_check_validates_every_exact_pin(tmp_path: Path):
    lock = tmp_path / "requirements.lock"
    lock.write_text(
        "alpha_pkg==1.2.3 --hash=sha256:abc\nbeta.pkg==2.0+cu129 --hash=sha256:def\n",
        encoding="utf-8",
    )
    versions = {"alpha-pkg": "1.2.3", "beta-pkg": "2.0+cu129"}
    findings = preflight.check_lock_packages(lock, version_getter=versions.__getitem__)
    assert findings[0].level == "ok"
    assert "all 2" in findings[0].message


def test_lock_check_reports_drift(tmp_path: Path):
    lock = tmp_path / "requirements.lock"
    lock.write_text("alpha==1.0 --hash=sha256:abc\n", encoding="utf-8")
    findings = preflight.check_lock_packages(lock, version_getter=lambda _name: "2.0")
    assert findings[0].level == "error"
    assert "2.0 (expected 1.0)" in findings[0].message


def test_torch_cuda_runtime_is_parsed_without_importing_torch(tmp_path: Path):
    version_file = tmp_path / "torch" / "version.py"
    version_file.parent.mkdir()
    version_file.write_text(
        "__version__ = '2.11.0+cu129'\ncuda = '12.9'\n", encoding="utf-8"
    )
    distribution = SimpleNamespace(locate_file=lambda relative: tmp_path / relative)
    findings = preflight.inspect_torch_cuda_runtime(
        PINNED, distribution_getter=lambda _name: distribution
    )
    assert [(item.level, item.check) for item in findings] == [
        ("ok", "cuda.wheel_runtime")
    ]


def test_model_config_accepts_expected_nested_architecture(tmp_path: Path):
    path = canonical_config_path(tmp_path)
    findings = preflight.check_model_config(PINNED, good_config(), path)
    assert {item.level for item in findings} == {"ok"}


def test_model_config_rejects_wrong_architecture_and_text_type(tmp_path: Path):
    config = good_config()
    config["architectures"] = ["Qwen3ForCausalLM"]
    config["text_config"] = {"model_type": "qwen3"}
    findings = preflight.check_model_config(
        PINNED, config, canonical_config_path(tmp_path)
    )
    errors = {item.check for item in findings if item.level == "error"}
    assert errors == {"model.architecture", "model.text_model_type"}


def test_arbitrary_sha_named_directory_does_not_prove_revision(tmp_path: Path):
    path = tmp_path / PINNED["model"]["revision"] / "config.json"
    path.parent.mkdir()
    path.write_text(json.dumps(good_config()), encoding="utf-8")
    findings = preflight.check_model_config(PINNED, good_config(), path)
    revision = next(item for item in findings if item.check == "model.revision")
    assert revision.level == "error"
    assert "canonical model-cache" in revision.message


def test_config_without_revision_evidence_is_non_green(tmp_path: Path):
    config = good_config()
    del config["_commit_hash"]
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    findings = preflight.check_model_config(PINNED, config, path)
    revision = next(item for item in findings if item.check == "model.revision")
    assert revision.level == "error"
    assert "exact revision is not proven" in revision.message


def test_snapshot_config_symlink_must_stay_inside_model_cache(tmp_path: Path):
    path = canonical_config_path(tmp_path)
    outside = tmp_path / "outside-config.json"
    outside.write_text(json.dumps(good_config()), encoding="utf-8")
    path.unlink()
    try:
        path.symlink_to(outside)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    findings = preflight.check_model_config(PINNED, good_config(), path)
    revision = next(item for item in findings if item.check == "model.revision")
    assert revision.level == "error"
    assert "escapes" in revision.message


def test_snapshot_config_symlink_may_resolve_to_internal_blob(tmp_path: Path):
    path = canonical_config_path(tmp_path)
    blob = path.parents[2] / "blobs" / "config-blob"
    blob.parent.mkdir()
    blob.write_text(json.dumps(good_config()), encoding="utf-8")
    path.unlink()
    try:
        path.symlink_to(blob)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    findings = preflight.check_model_config(PINNED, good_config(), path)
    revision = next(item for item in findings if item.check == "model.revision")
    assert revision.level == "ok"


@pytest.mark.parametrize(
    ("value", "expected_level", "expected_code"),
    [(None, "error", 1), ("", "ok", 0), ("2", "error", 1)],
)
def test_main_requires_explicitly_empty_cuda_visibility(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    value: str | None,
    expected_level: str,
    expected_code: int,
):
    if value is None:
        monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    else:
        monkeypatch.setenv("CUDA_VISIBLE_DEVICES", value)
    monkeypatch.setattr(
        preflight,
        "run_checks",
        lambda *_args: preflight.check_cuda_visibility(os.environ),
    )
    assert preflight.main(["unused", "--json"]) == expected_code
    result = json.loads(capsys.readouterr().out)
    assert result["compatible"] is (expected_code == 0)
    assert result["findings"][0]["level"] == expected_level


def test_hardware_is_always_left_pending():
    findings = preflight.hardware_pending_findings(PINNED)
    assert [item.level for item in findings] == ["pending", "pending"]
    assert {item.check for item in findings} == {"hardware.driver", "hardware.gpu"}


def test_checker_does_not_import_ml_or_hardware_modules():
    source = (ENV_DIR / "preflight.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert imported.isdisjoint(
        {"torch", "transformers", "tokenizers", "vllm", "pynvml"}
    )
