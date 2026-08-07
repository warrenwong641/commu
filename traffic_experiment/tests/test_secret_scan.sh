#!/usr/bin/env bash
# Automated secret-scan regression test.  Catches known credential patterns
# in source, templates, docs, and tests.  Placeholder values like
# "REPLACE_ME", "local-test-key", and "replace-with-a-random-local-value"
# are explicitly allowed.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
PASS=0; FAIL=0
pass() { printf 'PASS: %s\n' "$1"; PASS=$((PASS + 1)); }
fail() { printf 'FAIL: %s\n' "$1" >&2; FAIL=$((FAIL + 1)); }

# Patterns that indicate a real credential (not a placeholder).
# - hex strings ≥ 32 chars that look like tokens
# - bearer / api-key assignments with plausible values
# Exclude: REPLACE_ME, local-test-key, template placeholders, test fixtures.
SCAN_DIRS=(
  "${PROJECT_DIR}/scripts"
  "${PROJECT_DIR}/docs"
  "${PROJECT_DIR}/traffic_measure"
  "${PROJECT_DIR}/tests"
)

# Find potential secrets (long hex/base64 strings in assignments).
for d in "${SCAN_DIRS[@]}"; do
  while IFS=: read -r file line content; do
    # Skip allowed placeholders and test fixtures.
    case "${content}" in
      *REPLACE_ME*) continue ;;
      *local-test-key*) continue ;;
      *replace-with-a-random*) continue ;;
      *'${API_KEY}'*) continue ;;
      *'${LOCAL_VLLM_API_KEY}'*) continue ;;
      *'${OPENROUTER_API_KEY}'*) continue ;;
      *'${GEMINI_API_KEY}'*) continue ;;
      *api_key=\"k\"*) continue ;;  # test fixture
      *api_key=\"secret\"*) continue ;;  # test fixture
      *api_key=\"test-key\"*) continue ;;  # test fixture
      *API_KEY=\"\"*) continue ;;  # empty string default
    esac
    # Long hex/base64-like strings in assignment context.
    if echo "${content}" | grep -qE '[A-Za-z0-9+/=_-]{32,}'; then
      fail "${file}:${line}: ${content}"
    fi
  done < <(grep -rnE \
    '(API_KEY|api.key|api-key|Bearer|Authorization).*[A-Za-z0-9+/=_-]{32,}' \
    "${d}" --include="*.sh" --include="*.py" --include="*.md" --include="*.env" --include="*.template" 2>/dev/null || true)
done

echo ""
echo "secret-scan: ${PASS} passed, ${FAIL} failed"
[[ "${FAIL}" -eq 0 ]] && echo "SECRET_SCAN_OK" || { echo "SECRET_SCAN_FAILURES" >&2; exit 1; }
