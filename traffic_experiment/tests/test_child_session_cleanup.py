from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
LIB = ROOT / "scripts" / "lib.sh"


def _proc_exposes_child_processes() -> bool:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1)"])
    try:
        return Path(f"/proc/{child.pid}/stat").is_file()
    finally:
        child.terminate()
        child.wait()


@pytest.mark.skipif(
    os.name != "posix"
    or shutil.which("bash") is None
    or shutil.which("setsid") is None
    or not _proc_exposes_child_processes(),
    reason="the owned-session lifecycle requires Linux with child /proc visibility",
)
def test_owned_child_cleanup_covers_descendants_and_refuses_bad_identity(
    tmp_path: Path,
):
    env_file = tmp_path / "server.env"
    env_file.write_text("\n", encoding="utf-8")
    ready_file = tmp_path / "descendant.ready"
    child_code = (
        "import pathlib,subprocess,time;"
        "child=subprocess.Popen(['sleep','30']);"
        f"pathlib.Path({str(ready_file)!r}).write_text(str(child.pid));"
        "time.sleep(30)"
    )
    script = f"""
set -euo pipefail
export EXPERIMENT_ENV_FILE={shlex.quote(str(env_file))}
source {shlex.quote(str(LIB))}

(
  trap - INT TERM
  exec setsid {shlex.quote(sys.executable)} -c {shlex.quote(child_code)}
) >/dev/null 2>&1 &
child_pid=$!
child_ticks="$(record_owned_session_start_ticks "${{child_pid}}")"
for _ in $(seq 1 100); do
  [[ -s {shlex.quote(str(ready_file))} ]] && break
  sleep 0.01
done
[[ -s {shlex.quote(str(ready_file))} ]]

if stop_owned_child "${{child_pid}}" "$((child_ticks + 1))" test-worker; then
  echo "mismatched identity was accepted" >&2
  exit 1
fi
if ! kill -0 "${{child_pid}}" 2>/dev/null; then
  echo "mismatched identity disturbed the child" >&2
  exit 1
fi

stop_owned_child "${{child_pid}}" "${{child_ticks}}" test-worker
if owned_session_group_has_live_members "${{child_pid}}"; then
  echo "owned descendant survived process-group cleanup" >&2
  exit 1
fi
"""
    completed = subprocess.run(
        ["bash", "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
