#!/usr/bin/env bash
# Deploy step: build, sign, and install stdlib package + core trust store.
#
# Inputs (env):
#   REPO_ROOT       — repository root
#   DIST            — staged distribution directory
#   STAGE           — staging scratch directory (for temp files)
#   DRIFTC_VERSION  — compiler version string
#
# Signing key: DRIFT_SIGN_KEY_FILE or DRIFT_SIGN_KEY_CMD must be set.
#
# Outputs:
#   ${DIST}/lib/stdlib/std.dmp
#   ${DIST}/lib/stdlib/std.dmp.sig
#   ${DIST}/lib/compiler/lang/driftc/packages/core_trust.json
set -euo pipefail

: "${REPO_ROOT:?}"
: "${DIST:?}"
: "${STAGE:?}"
: "${DRIFTC_VERSION:?}"

if [[ -z "${DRIFT_SIGN_KEY_FILE:-}" && -z "${DRIFT_SIGN_KEY_CMD:-}" ]]; then
	echo "error: package signing key required." >&2
	echo "  set DRIFT_SIGN_KEY_FILE=/path/to/seed.key" >&2
	echo "  or  DRIFT_SIGN_KEY_CMD=\"command\"" >&2
	exit 1
fi

# ── Build stdlib package ─────────────────────────────────────────────
# --dev: allows compiling reserved-namespace modules (std.*, lang.*)
# --stdlib-root <empty>: prevents emission filter from skipping stdlib
#   modules (the filter skips modules whose source is under stdlib_root;
#   an empty root ensures no source matches, so all modules are included
#   in the package).
echo "[deploy] building stdlib package..."
STDLIB_DMP="${STAGE}/std.dmp"
EMPTY_STDLIB="${STAGE}/_empty_stdlib"
mkdir -p "${EMPTY_STDLIB}"
(cd "${REPO_ROOT}" && PYTHONPATH=. ./.venv/bin/python3 -m lang.driftc \
	--dev \
	--stdlib-root "${EMPTY_STDLIB}" \
	-M stdlib $(find stdlib -name '*.drift' | sort) \
	--package-id std \
	--package-version "${DRIFTC_VERSION}" \
	--package-target "drift-dev" \
	--emit-package "${STDLIB_DMP}" --json) >/dev/null

if [[ ! -f "${STDLIB_DMP}" ]]; then
	echo "error: stdlib package build produced no output" >&2
	exit 1
fi

# ── Sign ─────────────────────────────────────────────────────────────
echo "[deploy] signing stdlib package..."
(cd "${REPO_ROOT}" && PYTHONPATH=. ./.venv/bin/python3 -m lang.drift sign \
	"${STDLIB_DMP}" --include-pubkey)

if [[ ! -f "${STDLIB_DMP}.sig" ]]; then
	echo "error: signing produced no sidecar" >&2
	exit 1
fi

# ── Install ──────────────────────────────────────────────────────────
mkdir -p "${DIST}/lib/stdlib"
cp "${STDLIB_DMP}" "${DIST}/lib/stdlib/std.dmp"
cp "${STDLIB_DMP}.sig" "${DIST}/lib/stdlib/std.dmp.sig"

# ── Generate core trust store ────────────────────────────────────────
echo "[deploy] generating core trust store..."
CORE_TRUST="${DIST}/lib/compiler/lang/driftc/packages/core_trust.json"
(cd "${REPO_ROOT}" && PYTHONPATH=. ./.venv/bin/python3 \
	tools/deploy/gen_trust_store.py \
	--sidecar "${STDLIB_DMP}.sig" \
	--namespaces "std.*,lang.*,drift.*" \
	--output "${CORE_TRUST}")

# ── Verify outputs ───────────────────────────────────────────────────
fail=0
for f in "${DIST}/lib/stdlib/std.dmp" "${DIST}/lib/stdlib/std.dmp.sig" "${CORE_TRUST}"; do
	if [[ ! -f "${f}" ]]; then
		echo "error: expected output not found: ${f}" >&2
		fail=1
	fi
done
if [[ ${fail} -ne 0 ]]; then
	exit 1
fi

echo "[deploy] stdlib package installed and signed"
