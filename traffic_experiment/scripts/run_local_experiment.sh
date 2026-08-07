#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# vLLM must already be running in another terminal via 03_start_vllm.sh.
"${SCRIPT_DIR}/04_check_environment.sh"
"${SCRIPT_DIR}/05_run_profile.sh"
"${SCRIPT_DIR}/06_analyze_profile.sh"
