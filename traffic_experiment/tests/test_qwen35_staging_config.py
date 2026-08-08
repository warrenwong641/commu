from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
VALIDATOR = ROOT / "scripts" / "validate_qwen35_staging_config.py"
EXAMPLE = ROOT / "server.qwen35.staging.env.example"


def _run(config: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VALIDATOR), str(config)],
        check=False,
        capture_output=True,
        text=True,
    )


def _replace(directory: Path, old: str, new: str) -> Path:
    staged = directory / "staged.env"
    text = EXAMPLE.read_text(encoding="utf-8")
    assert old in text
    staged.write_text(text.replace(old, new, 1), encoding="utf-8")
    return staged


class Qwen35StagingConfigTest(unittest.TestCase):
    def test_tracked_staging_example_is_valid(self):
        result = _run(EXAMPLE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("QWEN35_STAGING_CONFIG_OK", result.stdout)

    def test_rejects_invariant_and_safety_violations(self):
        cases = [
            (
                'VLLM_MODEL="Qwen/Qwen3.5-9B"',
                'VLLM_MODEL="Qwen/Qwen3.5-9B-AWQ"',
                "VLLM_MODEL",
            ),
            (
                'VLLM_SERVED_MODEL_NAME="Qwen/Qwen3.5-9B"',
                'VLLM_SERVED_MODEL_NAME="qwen35"',
                "VLLM_SERVED_MODEL_NAME",
            ),
            (
                'VLLM_MODEL_REVISION="c202236235762e1c871ad0ccb60c8ee5ba337b9a"',
                'VLLM_MODEL_REVISION="main"',
                "VLLM_MODEL_REVISION",
            ),
            (
                'CUDA_VISIBLE_DEVICES="0,1"',
                'CUDA_VISIBLE_DEVICES="0,1,2"',
                "CUDA_VISIBLE_DEVICES",
            ),
            (
                'CUDA_VISIBLE_DEVICES="0,1"',
                'CUDA_VISIBLE_DEVICES="0,0"',
                "distinct GPU",
            ),
            (
                'PARALLEL_WORKERS="2"',
                'PARALLEL_WORKERS="3"',
                "CUDA_VISIBLE_DEVICES",
            ),
            ('VLLM_PORT="8000"', 'VLLM_PORT="0"', "VLLM_PORT"),
            ('VLLM_PORT="8000"', 'VLLM_PORT="70000"', "between 1 and 65535"),
            (
                'VLLM_SECONDARY_PORT="8001"',
                'VLLM_SECONDARY_PORT="8002"',
                "worker/port-step mapping",
            ),
            (
                'VLLM_PORT_STEP="1"',
                'VLLM_PORT_STEP="2"',
                "worker/port-step mapping",
            ),
            (
                'TENSOR_PARALLEL_SIZE="1"',
                'TENSOR_PARALLEL_SIZE="2"',
                "TENSOR_PARALLEL_SIZE",
            ),
            (
                'MAX_MODEL_LEN="65536"',
                'MAX_MODEL_LEN="32768"',
                "MAX_MODEL_LEN",
            ),
            (
                'MAX_OUTPUT_TOKENS="4096"',
                'MAX_OUTPUT_TOKENS="8192"',
                "MAX_OUTPUT_TOKENS",
            ),
            (
                'SUMMARY_MAX_OUTPUT_TOKENS="4096"',
                'SUMMARY_MAX_OUTPUT_TOKENS="1024"',
                "SUMMARY_MAX_OUTPUT_TOKENS",
            ),
            (
                'VLLM_HOST="127.0.0.1"',
                'VLLM_HOST="0.0.0.0"',
                "loopback-only",
            ),
            (
                'VLLM_HOST="127.0.0.1"',
                'VLLM_HOST="192.0.2.10"',
                "loopback-only",
            ),
            (
                'VLLM_BIN="__STAGED_VLLM_BIN__"',
                'VLLM_BIN="vllm"',
                "absolute staged",
            ),
            (
                'RUNNER_PYTHON="__STAGED_RUNNER_PYTHON__"',
                'RUNNER_PYTHON="python3"',
                "absolute staged",
            ),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for old, new, expected_error in cases:
                with self.subTest(new=new):
                    result = _run(_replace(directory, old, new))
                    self.assertEqual(result.returncode, 2)
                    self.assertIn(expected_error, result.stderr)

    def test_rejects_credential_assignments(self):
        assignments = [
            'LOCAL_VLLM_API_KEY="dummy"',
            'HF_TOKEN="dummy"',
            'export HF_TOKEN="dummy"',
            'ADMIN_PASSWORD="dummy"',
            'SERVICE_SECRET="dummy"',
            'LAB_CREDENTIAL="dummy"',
        ]
        with tempfile.TemporaryDirectory() as temporary:
            staged = Path(temporary) / "staged.env"
            for assignment in assignments:
                with self.subTest(assignment=assignment):
                    staged.write_text(
                        EXAMPLE.read_text(encoding="utf-8")
                        + f"\n{assignment}\n",
                        encoding="utf-8",
                    )
                    result = _run(staged)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("credential assignment", result.stderr)

    def test_accepts_explicit_absolute_staged_paths(self):
        text = EXAMPLE.read_text(encoding="utf-8")
        text = text.replace(
            'VLLM_BIN="__STAGED_VLLM_BIN__"',
            'VLLM_BIN="/srv/commu-staging/vllm/bin/vllm"',
        ).replace(
            'RUNNER_PYTHON="__STAGED_RUNNER_PYTHON__"',
            'RUNNER_PYTHON="/srv/commu-staging/runner/bin/python"',
        )
        with tempfile.TemporaryDirectory() as temporary:
            staged = Path(temporary) / "staged.env"
            staged.write_text(text, encoding="utf-8")
            result = _run(staged)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_shell_syntax_without_executing_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            marker = directory / "must-not-exist"
            staged = _replace(
                directory,
                'VLLM_BIN="__STAGED_VLLM_BIN__"',
                f'VLLM_BIN="$(touch {marker})"',
            )
            result = _run(staged)
            self.assertEqual(result.returncode, 2)
            self.assertIn("shell expansion", result.stderr)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
