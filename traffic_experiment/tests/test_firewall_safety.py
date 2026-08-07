from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).parents[1] / "scripts" / "25_physical_firewall.sh"
).read_text(encoding="utf-8")


def test_firewall_apply_requires_explicit_narrow_client_cidrs():
    assert 'CLIENT_CIDRS="${CLIENT_CIDRS:-}"' in SCRIPT
    assert "apply requires explicit CLIENT_CIDRS" in SCRIPT
    assert "IPv4 /24+ or IPv6 /64+" in SCRIPT
    assert "144.214.0.0/16" not in SCRIPT
    assert "0.0.0.0/0" not in SCRIPT


def test_ufw_failures_are_not_suppressed_and_state_is_verified():
    ufw_lines = [
        line
        for line in SCRIPT.splitlines()
        if re.search(r"\bufw\b", line) and not line.lstrip().startswith("#")
    ]
    assert all("|| true" not in line for line in ufw_lines)
    assert 'grep -Fq "Status: active"' in SCRIPT
    assert "applied and verified" in SCRIPT
    assert "cleanup complete; pre-transaction profile rules preserved" in SCRIPT


def test_cleanup_and_rollback_are_scoped_to_selected_listener_profile():
    assert (
        'RULE_TAG_PREFIX="commu-physical-client-${LISTENER_PROFILE}-${PHYS_IF}"'
        in SCRIPT
    )
    assert 'grep -F "${RULE_TAG_PREFIX}-"' in SCRIPT
    assert "transaction_exit" in SCRIPT
    assert "delete_rule_exact" in SCRIPT
    assert 'ufw --force delete allow in on "${PHYS_IF}"' in SCRIPT
    assert "rollback complete; pre-transaction profile rules preserved" in SCRIPT
    assert "flock -n 9" in SCRIPT
    assert 'delete "${num}"' not in SCRIPT


def test_firewall_logs_are_repo_relative_and_rules_bind_destination_interface():
    assert "/home/wongshingyin" not in SCRIPT
    assert 'EXPERIMENT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"' in SCRIPT
    assert (
        'LOG_DIR="${FIREWALL_LOG_DIR:-${RUNS_ABS}/physical_validation/'
        'server/${LISTENER_PROFILE}}"'
        in SCRIPT
    )
    assert 'PHYS_IF="${PHYS_IF:-ens20f0}"' in SCRIPT
    assert 'ip -4 -o addr show dev "${PHYS_IF}" scope global' in SCRIPT
    assert 'PHYS_IP ${PHYS_IP} is not assigned to ${PHYS_IF}' in SCRIPT
    assert SCRIPT.count('ufw allow in on "${PHYS_IF}"') == 1
    assert 'ufw --force delete allow in on "${PHYS_IF}"' in SCRIPT
    assert SCRIPT.count('to "${PHYS_IP}"') >= 2
    assert "to any" not in SCRIPT


def test_firewall_mutation_is_transactional_and_refuses_symlink_targets():
    baseline_index = SCRIPT.index('ufw status numbered >"${BASELINE_FILE}"')
    state_index = SCRIPT.index("write_state applying", baseline_index)
    arm_index = SCRIPT.index("arm_transaction apply", state_index)
    mutation_index = SCRIPT.index('ufw allow in on "${PHYS_IF}"', arm_index)
    disarm_index = SCRIPT.index("disarm_transaction", mutation_index)
    assert baseline_index < state_index < arm_index < mutation_index < disarm_index
    assert "trap transaction_exit EXIT" in SCRIPT
    assert "trap handle_interrupt INT" in SCRIPT
    assert "trap handle_termination TERM" in SCRIPT
    assert "baseline_restored" in SCRIPT
    assert "refuse_symlinked_targets" in SCRIPT
    assert '[[ -L "${STATE_FILE}" ]]' in SCRIPT
    assert '! -f "${BASELINE_FILE}" || -L "${BASELINE_FILE}"' in SCRIPT


@pytest.mark.skipif(
    os.name != "posix"
    or shutil.which("bash") is None
    or shutil.which("flock") is None,
    reason="the firewall helper is Linux-specific",
)
def test_exact_cleanup_preserves_preexisting_same_profile_rule(tmp_path: Path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log_dir = tmp_path / "firewall"
    log_dir.mkdir()
    fake_state = tmp_path / "ufw-state.txt"
    fake_calls = tmp_path / "ufw-calls.txt"
    prefix = "commu-physical-client-standard_https-eth0"
    token = "a" * 64
    old_tag = f"{prefix}-preexisting-tls"
    tls_tag = f"{prefix}-{token[:12]}-tls"
    h3_tag = f"{prefix}-{token[:12]}-h3"
    old_rule = (
        f"[ 1] 192.0.2.10 443/tcp on eth0 ALLOW IN "
        f"203.0.113.99 # {old_tag}"
    )
    tls_rule = (
        f"[ 2] 192.0.2.10 443/tcp on eth0 ALLOW IN "
        f"203.0.113.42 # {tls_tag}"
    )
    h3_rule = (
        f"[ 3] 192.0.2.10 443/udp on eth0 ALLOW IN "
        f"203.0.113.42 # {h3_tag}"
    )
    fake_state.write_text(
        "\n".join(("Status: active", old_rule, tls_rule, h3_rule, "")),
        encoding="utf-8",
    )
    baseline = log_dir / "ufw_before.20260807T000000.123.txt"
    baseline.write_text(
        "\n".join(("Status: active", old_rule, "")),
        encoding="utf-8",
    )
    ownership = log_dir / "firewall.state"
    ownership.write_text(
        "\n".join(
            (
                "owner=commu-physical-firewall-v1",
                "status=active",
                f"owner_token={token}",
                "listener_profile=standard_https",
                "physical_interface=eth0",
                "physical_ip=192.0.2.10",
                f"baseline_file={baseline}",
                f"rule=tcp|443|203.0.113.42/32|{tls_tag}",
                f"rule=udp|443|203.0.113.42/32|{h3_tag}",
                "",
            )
        ),
        encoding="utf-8",
    )
    fake_ufw = fake_bin / "ufw"
    fake_ufw.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"${FAKE_UFW_CALLS}"
if [[ "${1:-}" == "status" ]]; then
  cat "${FAKE_UFW_STATE}"
  exit 0
fi
if [[ "${1:-}" == "--force" && "${2:-}" == "delete" ]]; then
  tag="${!#}"
  awk -v tag="${tag}" 'index($0, tag) == 0' "${FAKE_UFW_STATE}" \
    >"${FAKE_UFW_STATE}.tmp"
  mv "${FAKE_UFW_STATE}.tmp" "${FAKE_UFW_STATE}"
  exit 0
fi
echo "unexpected fake ufw invocation: $*" >&2
exit 1
""",
        encoding="utf-8",
    )
    fake_ufw.chmod(0o755)

    script_path = Path(__file__).parents[1] / "scripts" / "25_physical_firewall.sh"
    shell = f"""
set -euo pipefail
export PATH={shlex.quote(str(fake_bin))}:"${{PATH}}"
export FAKE_UFW_STATE={shlex.quote(str(fake_state))}
export FAKE_UFW_CALLS={shlex.quote(str(fake_calls))}
export FIREWALL_LOG_DIR={shlex.quote(str(log_dir))}
export LISTENER_PROFILE=standard_https
export PHYS_IF=eth0
source {shlex.quote(str(script_path))} status >/dev/null
require_root() {{ :; }}
cleanup_fw
"""
    completed = subprocess.run(
        ["bash", "-c", shell],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    remaining = fake_state.read_text(encoding="utf-8")
    assert old_tag in remaining
    assert tls_tag not in remaining
    assert h3_tag not in remaining
    assert not ownership.exists()
    calls = fake_calls.read_text(encoding="utf-8")
    assert "--force delete allow in on eth0 proto tcp" in calls
    assert "--force delete allow in on eth0 proto udp" in calls
    assert "--force delete 1" not in calls
