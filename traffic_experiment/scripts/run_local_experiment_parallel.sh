#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

"${SCRIPT_DIR}/04_check_environment_parallel.sh"
"${SCRIPT_DIR}/05_run_profile_parallel.sh"
"${SCRIPT_DIR}/06_analyze_profile.sh"
