from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "scripts"


def _script(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8")


def test_all_protocol_entrypoints_share_one_admission_definition():
    for name in (
        "18_run_lab_matrix.sh",
        "19_run_lab_sessions.sh",
        "22_validate_protocol_pilots.sh",
    ):
        assert 'source "${SCRIPT_DIR}/protocol_admission.sh"' in _script(name)

    admission = " ".join(
        _script("protocol_admission.sh").replace("\\\n", " ").split()
    )
    for key in (
        "schema",
        "status",
        "qa_manifest_sha256",
        "summary_manifest_sha256",
        "model_revision",
        "model_name",
        "network_mtu",
        "caddy_version",
        "protocol_stack_sha256",
    ):
        assert f'require_protocol_marker_match "${{marker}}" {key}' in admission
    assert "schema=commu-protocol-admission-v1" in admission
    assert "write_protocol_success_marker" in admission


def test_protocol_digest_binds_per_request_and_warm_session_paths():
    admission = _script("protocol_admission.sh")
    for path in (
        "scripts/08_run_transport_profile.sh",
        "scripts/08_run_transport_profile_parallel.sh",
        "scripts/14_run_warm_session.sh",
        "scripts/18_run_lab_matrix.sh",
        "scripts/19_run_lab_sessions.sh",
        "scripts/22_validate_protocol_pilots.sh",
        "scripts/protocol_admission.sh",
        "traffic_measure/runner.py",
        "traffic_measure/http3_client.py",
        "traffic_measure/pilot_evidence.py",
        "traffic_measure/session_timeline.py",
        "traffic_measure/vllm_metrics.py",
    ):
        assert path in admission


def test_admission_revalidates_immutable_per_pilot_result_and_pcap_evidence():
    admission = _script("protocol_admission.sh")
    pilots = _script("22_validate_protocol_pilots.sh")

    assert 'http3) printf \'(udp port 8444 or tcp port 8444)' in admission
    assert "verify_protocol_pilot_reference" in admission
    assert "tls_pilot_marker_sha256" in admission
    assert "http3_pilot_marker_sha256" in admission
    assert "protocol_pilot_evidence verify" in admission
    assert "run_or_validate_strict tls13 8443 tls" in pilots
    assert "run_or_validate_strict http3 8444 http3" in pilots
    assert "run_or_validate()" not in pilots
    assert "CAPTURE_FILTER_OVERRIDE" in pilots
    assert "PILOT_EVIDENCE_OK.json" in pilots


def test_measured_orchestrators_gate_before_network_mutation():
    for name in ("18_run_lab_matrix.sh", "19_run_lab_sessions.sh"):
        script = _script(name)
        preflight = script.index('"${SCRIPT_DIR}/17_lab_preflight.sh"')
        admission = script.index("verify_protocol_admission", preflight)
        network_loop = script.index("for network in", admission)
        apply_network = script.index("claim_and_apply_network", network_loop)
        assert preflight < admission < network_loop < apply_network


def test_outer_network_ownership_is_set_only_after_transactional_apply():
    for name in (
        "18_run_lab_matrix.sh",
        "19_run_lab_sessions.sh",
        "22_validate_protocol_pilots.sh",
    ):
        script = _script(name)
        apply_index = script.index(
            '"${SCRIPT_DIR}/11_network_condition.sh" apply'
        )
        owned_index = script.index("NETWORK_OWNED=1", apply_index)
        assert apply_index < owned_index
