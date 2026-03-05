#!/usr/bin/env bash
# Deploy step: compile and run smoke test using staged distribution.
#
# Inputs (env):
#   DIST            — staged distribution directory
#   REPO_ROOT       — repository root (for smoke source)
#   STAGE           — staging scratch directory (for temp binaries)
#   DRIFT_PYTHON    — (optional) Python interpreter override
#
# Exercises the full signed-package path: wrapper → compiler → package
# load → signature verification → compile → link → run.
set -euo pipefail

: "${DIST:?}"
: "${REPO_ROOT:?}"
: "${STAGE:?}"

SMOKE_SRC="${REPO_ROOT}/tools/deploy/smoke_test.drift"
SMOKE_BIN="${STAGE}/smoke_test_bin"

# Resolve the Python the wrapper will use (same logic as wrapper).
SMOKE_PYTHON="${DRIFT_PYTHON:-}"
if [[ -z "${SMOKE_PYTHON}" ]]; then
	SMOKE_PYTHON="$(command -v python3 2>/dev/null || command -v python 2>/dev/null || true)"
fi
if [[ -z "${SMOKE_PYTHON}" ]]; then
	echo "error: python3 not found; set DRIFT_PYTHON or add python3 to PATH" >&2
	exit 1
fi

# Verify required Python packages are available in the target interpreter.
echo "[deploy] checking Python prerequisites (${SMOKE_PYTHON})..."
MISSING_DEPS=""
for dep in lark llvmlite cryptography; do
	if ! "${SMOKE_PYTHON}" -c "import ${dep}" 2>/dev/null; then
		MISSING_DEPS="${MISSING_DEPS} ${dep}"
	fi
done
if [[ -n "${MISSING_DEPS}" ]]; then
	echo "error: target Python (${SMOKE_PYTHON}) is missing required packages:${MISSING_DEPS}" >&2
	echo "  install with: ${SMOKE_PYTHON} -m pip install${MISSING_DEPS}" >&2
	echo "  or set DRIFT_PYTHON to a Python that has them" >&2
	exit 1
fi

echo "[deploy] running smoke test..."
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
