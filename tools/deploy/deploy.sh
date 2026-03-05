#!/usr/bin/env bash
# Drift distribution deploy — thin orchestrator.
#
# Usage: tools/deploy/deploy.sh <DEST>
#
# Builds a versioned, self-contained Drift distribution under DEST:
#   DEST/drift-<VERSION>+abi<ABI>/  (bin, lib, doc, examples)
#   DEST/current -> drift-<VERSION>+abi<ABI>  (atomic symlink)
#
# Steps (each is a standalone script under tools/deploy/):
#   1. step_bundle.sh    — stage compiler, runtime, wrapper, docs
#   2. step_stdlib_pkg.sh — build + sign stdlib package, core trust store
#   3. step_smoke.sh     — compile + run smoke test using deployed paths
#   4. step_publish.sh   — atomic publish + symlink switch
#
# Requires DRIFT_SIGN_KEY_FILE or DRIFT_SIGN_KEY_CMD for stdlib signing.
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${DEPLOY_DIR}/../.." && pwd)"

# ── Args ──────────────────────────────────────────────────────────────
if [[ $# -lt 1 ]]; then
	echo "usage: $0 <DEST>" >&2
	exit 1
fi

DEST="$(cd "$(dirname "$1")" 2>/dev/null && pwd)/$(basename "$1")" || {
	mkdir -p "$1"
	DEST="$(cd "$1" && pwd)"
}

# ── Signing key ───────────────────────────────────────────────────────
if [[ -z "${DRIFT_SIGN_KEY_FILE:-}" && -z "${DRIFT_SIGN_KEY_CMD:-}" ]]; then
	echo "error: package signing key required." >&2
	echo "  set DRIFT_SIGN_KEY_FILE=/path/to/seed.key" >&2
	echo "  or  DRIFT_SIGN_KEY_CMD=\"command\"" >&2
	exit 1
fi

# ── Version metadata ─────────────────────────────────────────────────
export DRIFTC_VERSION="$(cd "${REPO_ROOT}" && PYTHONPATH=. ./.venv/bin/python3 -c \
	"from lang.driftc.driftc_versions import DRIFTC_VERSION; print(DRIFTC_VERSION)")"
export ABI_VERSION="$(cd "${REPO_ROOT}" && PYTHONPATH=. ./.venv/bin/python3 -c \
	"from lang.driftc.driftc_versions import DRIFT_RT_ABI_VERSION; print(DRIFT_RT_ABI_VERSION)")"
export GIT_COMMIT="$(git -C "${REPO_ROOT}" rev-parse --short HEAD 2>/dev/null || echo "unknown")"
export GIT_COMMIT_FULL="$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || echo "unknown")"
export BUILD_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
export HOST_PLATFORM="$(uname -s | tr '[:upper:]' '[:lower:]')"
export HOST_ARCH="$(uname -m)"

export VERSION_DIR="drift-${DRIFTC_VERSION}+abi${ABI_VERSION}"

echo "[deploy] version:  ${DRIFTC_VERSION}"
echo "[deploy] abi:      ${ABI_VERSION}"
echo "[deploy] commit:   ${GIT_COMMIT}"
echo "[deploy] dest:     ${DEST}/${VERSION_DIR}"

# ── Detect clang ──────────────────────────────────────────────────────
export CLANG="$(command -v clang-15 2>/dev/null || command -v clang 2>/dev/null || true)"
if [[ -z "${CLANG}" ]]; then
	echo "error: clang not found" >&2
	exit 1
fi

# ── Build runtime archives ───────────────────────────────────────────
echo "[deploy] building runtime archives..."
(cd "${REPO_ROOT}" && DRIFT_RUNTIME_CLANG="${CLANG}" PYTHONPATH=. ./.venv/bin/python3 -c "
from pathlib import Path; import os
from lang.language_runtime import build_runtime_archive
root = Path('.').resolve()
clang = os.environ['DRIFT_RUNTIME_CLANG']
for v in ('default','debug','asan','alloc_track','optimized'):
    build_runtime_archive(root, clang=clang, variant=v)
") >/dev/null

# ── Staging ───────────────────────────────────────────────────────────
mkdir -p "${DEST}"
export STAGE="$(mktemp -d "${DEST}/.deploy-staging.XXXXXX")"
trap 'rm -rf "${STAGE}"' EXIT

export DIST="${STAGE}/${VERSION_DIR}"
export REPO_ROOT
export DEST

# ── Steps ─────────────────────────────────────────────────────────────
"${DEPLOY_DIR}/step_bundle.sh"
"${DEPLOY_DIR}/step_stdlib_pkg.sh"
"${DEPLOY_DIR}/step_smoke.sh"
"${DEPLOY_DIR}/step_publish.sh"

trap - EXIT

echo ""
echo "  export PATH=\"${DEST}/current/bin:\$PATH\""
echo ""
