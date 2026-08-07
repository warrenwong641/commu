from __future__ import annotations

from pathlib import Path


SCRIPT = (
    Path(__file__).parents[1] / "scripts" / "17_lab_preflight.sh"
).read_text(encoding="utf-8")


def test_lab_preflight_requires_exact_model_commit_before_success():
    assert '^[0-9a-fA-F]{40}$' in SCRIPT
    assert "must be an exact 40-character Hugging Face commit SHA" in SCRIPT
    assert "model_revision=${VLLM_MODEL_REVISION}" in SCRIPT
    assert "model_revision=${VLLM_MODEL_REVISION:-UNPINNED}" not in SCRIPT
    assert "WARNING: VLLM_MODEL_REVISION is unpinned" not in SCRIPT


def test_lab_preflight_requires_expected_frozen_manifest_digests():
    assert "MANIFEST_SHA256 must be the expected 64-character SHA-256" in SCRIPT
    assert "SUMMARY_MANIFEST_SHA256 must be the expected 64-character SHA-256" in SCRIPT
    assert "Frozen QA manifest digest mismatch" in SCRIPT
    assert "Frozen summary manifest digest mismatch" in SCRIPT
