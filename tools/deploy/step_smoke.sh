#!/usr/bin/env bash
# Deploy step: compile and run smoke test using staged distribution.
#
# Inputs (env):
#   DIST            — staged distribution directory
#   REPO_ROOT       — repository root (for smoke source)
#   STAGE           — staging scratch directory (for temp binaries)
#
# Exercises the full signed-package path: PEX entry → compiler → package
# load → signature verification → compile → link → run.
set -euo pipefail

: "${DIST:?}"
: "${REPO_ROOT:?}"
: "${STAGE:?}"

SMOKE_SRC="${REPO_ROOT}/tools/deploy/smoke_test.drift"
SMOKE_BIN="${STAGE}/smoke_test_bin"

echo "[deploy] running smoke test with deployed PEX executable..."
"${DIST}/bin/driftc" "${SMOKE_SRC}" -o "${SMOKE_BIN}" 2>&1 | tail -5
SMOKE_EXIT=0
"${SMOKE_BIN}" > "${STAGE}/smoke_stdout" 2>&1 || SMOKE_EXIT=$?

SMOKE_STDOUT="$(cat "${STAGE}/smoke_stdout")"
if [[ ${SMOKE_EXIT} -ne 42 ]]; then
	echo "error: smoke test failed (expected exit 42, got ${SMOKE_EXIT})" >&2
	echo "stdout: ${SMOKE_STDOUT}" >&2
	exit 1
fi
if [[ "${SMOKE_STDOUT}" != "drift deploy smoke ok" ]]; then
	echo "error: smoke test stdout mismatch" >&2
	echo "expected: drift deploy smoke ok" >&2
	echo "got:      ${SMOKE_STDOUT}" >&2
	exit 1
fi
echo "[deploy] smoke test passed (exit=42, stdout ok)"

# Clean any build artifacts the smoke test left inside the deployed tree.
find "${DIST}" -name '.build.lock' -delete
