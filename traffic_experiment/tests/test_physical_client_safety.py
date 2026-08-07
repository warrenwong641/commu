from __future__ import annotations

import hashlib
import json
from pathlib import Path

from traffic_experiment.traffic_measure.cli import _repair_results
from traffic_experiment.traffic_measure.common import read_jsonl


EXPERIMENT_ROOT = Path(__file__).parents[1]
CLIENT_SCRIPT = (
    EXPERIMENT_ROOT / "scripts" / "run_physical_client.sh"
).read_text(encoding="utf-8")
BUNDLE_SCRIPT = (
    EXPERIMENT_ROOT / "scripts" / "24_create_client_bundle.sh"
).read_text(encoding="utf-8")


def test_physical_transport_aliases_are_normalized_once():
    assert "tls | tls13)" in CLIENT_SCRIPT
    assert 'CLI_TRANSPORT="tls13"' in CLIENT_SCRIPT
    assert "h3 | http3)" in CLIENT_SCRIPT
    assert 'CLI_TRANSPORT="http3"' in CLIENT_SCRIPT
    assert '--transport "${CLI_TRANSPORT}"' in CLIENT_SCRIPT
    assert '--transport "${TRANSPORT_INPUT}"' not in CLIENT_SCRIPT
    assert "--connection-mode cold" in CLIENT_SCRIPT


def test_preflight_and_calibration_are_outside_capture():
    capture_start = CLIENT_SCRIPT.index(
        '"${DUMPCAP_CMD}" -q -i "${CAPTURE_IF}"'
    )
    assert CLIENT_SCRIPT.index("Checking authenticated TLS endpoint") < capture_start
    assert CLIENT_SCRIPT.index("Calibrating TCP reachability before capture") < capture_start
    assert CLIENT_SCRIPT.count(
        '-m traffic_experiment.traffic_measure.cli run'
    ) == 1


def test_physical_result_binding_is_dynamic_and_strict():
    assert "conv-30::77::no_compression" not in CLIENT_SCRIPT
    assert '--request-id "${REQUEST_ID}"' in CLIENT_SCRIPT
    assert 'row.get("transport") != transport' in CLIENT_SCRIPT
    assert 'expected_http = "3" if transport == "http3" else "1.1"' in CLIENT_SCRIPT
    assert 'require_positive_count "TLS"' in CLIENT_SCRIPT
    assert 'require_positive_count "QUIC"' in CLIENT_SCRIPT
    assert 'require_positive_count "uplink"' in CLIENT_SCRIPT
    assert 'require_positive_count "downlink"' in CLIENT_SCRIPT
    assert "TCP fallback packets" in CLIENT_SCRIPT
    assert '"${OUTPUT_DIR}/VALIDATION_COMPLETE"' in CLIENT_SCRIPT
    assert 'marker.get("model") == model' in CLIENT_SCRIPT
    assert 'marker.get("manifest_sha256") == manifest_sha256' in CLIENT_SCRIPT
    assert 'row.get("manifest_sha256") == manifest_sha256' in CLIENT_SCRIPT
    assert (
        'marker.get("measurement_config_sha256") == config_sha256'
        in CLIENT_SCRIPT
    )
    assert '"seed": 42' in CLIENT_SCRIPT
    assert '"temperature": 0' in CLIENT_SCRIPT
    assert '"max_output_tokens": 4096' in CLIENT_SCRIPT


def test_external_repair_records_capture_provenance(tmp_path):
    results = tmp_path / "results.jsonl"
    pcap = tmp_path / "pilot.pcapng"
    pcap.write_bytes(b"pcap-evidence")
    row = {
        "request_id": "conversation-1::q1::no_compression",
        "condition": "no_compression",
        "transport": "tls13",
        "completed": True,
        "capture_file": None,
        "capture_sha256": None,
        "capture_may_be_truncated": True,
    }
    results.write_text(json.dumps(row) + "\n", encoding="utf-8")

    rc = _repair_results(
        results_path=results,
        pcap_path=pcap,
        request_id=row["request_id"],
        condition="no_compression",
        transport="tls13",
        capture_interface="eth0",
        capture_filter="tcp port 8443",
        manifest_sha256="a" * 64,
        measurement_config_sha256="b" * 64,
    )

    assert rc == 0
    repaired = read_jsonl(results)[0]
    assert repaired["capture_file"] == str(pcap)
    assert repaired["capture_sha256"] == hashlib.sha256(
        pcap.read_bytes()
    ).hexdigest()
    assert repaired["capture_interface"] == "eth0"
    assert repaired["capture_filter"] == "tcp port 8443"
    assert repaired["capture_return_code"] == 0
    assert repaired["capture_may_be_truncated"] is False
    assert repaired["external_capture"] is True
    assert repaired["manifest_sha256"] == "a" * 64
    assert repaired["measurement_config_sha256"] == "b" * 64
    assert repaired["repair_attached_pcap"] is True
    audit = read_jsonl(tmp_path / "repair_audit.jsonl")[0]
    assert audit["capture_interface"] == "eth0"
    assert audit["capture_filter"] == "tcp port 8443"
    assert audit["manifest_sha256"] == "a" * 64


def test_bundle_requires_and_copies_frozen_runtime_inputs():
    assert "Frozen QA manifest is required and must be nonempty" in BUNDLE_SCRIPT
    assert '[[ ! -s "${QA_MANIFEST}" ]]' in BUNDLE_SCRIPT
    assert '"${EXPERIMENT_ROOT}/traffic_measure"' in BUNDLE_SCRIPT
    assert '"${REPOSITORY_ROOT}/locomo_eval"' in BUNDLE_SCRIPT
    assert "-type f -name '*.py'" in BUNDLE_SCRIPT
    assert "traffic_experiment/artifacts/requests_32.jsonl" in BUNDLE_SCRIPT
    assert "FROZEN_ARTIFACTS.sha256" in BUNDLE_SCRIPT
    assert "gzip -n" in BUNDLE_SCRIPT
    assert "Created: $(date" not in BUNDLE_SCRIPT
    assert (
        'sha256sum "${BUNDLE_NAME}.tar.gz" '
        '>"${BUNDLE_NAME}.tar.gz.sha256"'
    ) in BUNDLE_SCRIPT
    assert "API_KEY=" not in BUNDLE_SCRIPT
    assert 'read -rsp "vLLM API key: " LOCAL_VLLM_API_KEY' in BUNDLE_SCRIPT
    assert 'export LOCAL_VLLM_API_KEY="${API_KEY}"' not in CLIENT_SCRIPT
    assert (
        "Set LOCAL_VLLM_API_KEY in the current process environment."
        in CLIENT_SCRIPT
    )
