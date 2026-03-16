#!/usr/bin/env bash
# Drift distribution deploy — thin orchestrator.
#
# Usage:
#   tools/deploy/deploy.sh --dest <DEST> [--python <PYTHON>]
#   tools/deploy/deploy.sh <DEST> [PYTHON=/path/to/python3]
#
# Builds a versioned, self-contained Drift distribution under DEST:
#   DEST/drift-<VERSION>+abi<ABI>/  (bin, lib, doc, examples)
#   DEST/current -> drift-<VERSION>+abi<ABI>  (atomic symlink)
#
# Steps (each is a standalone script under tools/deploy/):
#   1. step_build_pex.sh        — build PEX --scie eager executable (bin/driftc)
#   2. step_build_deploy_pex.sh — build PEX --scie eager executable (bin/drift)
#   3. step_bundle.sh           — stage compiler sources, runtime archives, docs
#   4. step_stdlib_pkg.sh       — build + sign stdlib package, core trust store
#   5. step_smoke.sh            — compile + run smoke test using deployed paths
#   6. step_publish.sh          — atomic publish + symlink switch
#
# Requires DRIFT_SIGN_KEY_FILE or DRIFT_SIGN_KEY_CMD for stdlib signing.
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${DEPLOY_DIR}/../.." && pwd)"

usage() {
	cat >&2 <<'USAGE_EOF'
usage:
  tools/deploy/deploy.sh --dest <DEST> [--python <PYTHON>]
  tools/deploy/deploy.sh <DEST> [PYTHON=/path/to/python3]

options:
  -d, --dest <DEST>      Deploy destination root (required)
  -p, --python <PYTHON>  Python interpreter for smoke/prereq checks (optional)
  -h, --help             Show this help
USAGE_EOF
}

# ── Args ──────────────────────────────────────────────────────────────
DEST_ARG=""
PYTHON_ARG=""
# `just deploy -- ...` forwards a leading `--`; drop it for normal parsing.
if [[ $# -gt 0 && "$1" == "--" ]]; then
	shift
fi
if [[ $# -eq 1 && "$1" != -* ]]; then
	DEST_ARG="$1"
else
	PARSED="$(getopt -o d:p:h --long dest:,python:,help -- "$@")" || {
		usage
		exit 2
	}
	eval set -- "${PARSED}"
	while true; do
		case "$1" in
			-d|--dest)
				DEST_ARG="$2"
				shift 2
				;;
			-p|--python)
				PYTHON_ARG="$2"
				shift 2
				;;
			-h|--help)
				usage
				exit 0
				;;
			--)
				shift
				break
				;;
			*)
				usage
				exit 2
				;;
		esac
	done
	for tok in "$@"; do
		case "${tok}" in
			-h|--help)
				usage
				exit 0
				;;
			DEST=*)
				DEST_ARG="${tok#DEST=}"
				;;
			PYTHON=*)
				PYTHON_ARG="${tok#PYTHON=}"
				;;
			*)
				if [[ -z "${DEST_ARG}" ]]; then
					DEST_ARG="${tok}"
				else
					echo "error: unexpected positional argument: ${tok}" >&2
					usage
					exit 2
				fi
				;;
		esac
	done
fi
if [[ -z "${DEST_ARG}" ]]; then
	echo "error: destination is required" >&2
	usage
	exit 2
fi
if [[ -n "${PYTHON_ARG}" ]]; then
	export DRIFT_PYTHON="${PYTHON_ARG}"
fi

# Normalize destination path without relying on parent existence.
# Supports:
# - "~" and "~/..."
# - relative paths (resolved against current working directory)
# - absolute paths
DEST_RAW="${DEST_ARG}"
if [[ "${DEST_RAW}" == "~" ]]; then
	DEST_RAW="${HOME}"
elif [[ "${DEST_RAW}" == "~/"* ]]; then
	DEST_RAW="${HOME}/${DEST_RAW#"~/"}"
fi
if [[ "${DEST_RAW}" != /* ]]; then
	DEST_RAW="${PWD}/${DEST_RAW}"
fi
# Trim trailing slash (except root).
DEST="${DEST_RAW%/}"
if [[ -z "${DEST}" ]]; then
	DEST="/"
fi

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

# ── Prerequisite checks ──────────────────────────────────────────────
export CLANG="$(command -v clang 2>/dev/null || true)"
if [[ -z "${CLANG}" ]]; then
	echo "error: clang not found in PATH" >&2
	exit 1
fi

PEX_CMD="${REPO_ROOT}/.venv/bin/pex"
if [[ ! -x "${PEX_CMD}" ]]; then
	echo "error: pex not found at ${PEX_CMD}" >&2
	echo "  install into the project venv:" >&2
	echo "    ./.venv/bin/pip install pex" >&2
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
"${DEPLOY_DIR}/step_build_pex.sh"
"${DEPLOY_DIR}/step_build_deploy_pex.sh"
"${DEPLOY_DIR}/step_bundle.sh"
"${DEPLOY_DIR}/step_stdlib_pkg.sh"
"${DEPLOY_DIR}/step_smoke.sh"
"${DEPLOY_DIR}/step_publish.sh"

trap - EXIT

echo ""
echo "  export PATH=\"${DEST}/current/bin:\$PATH\""
echo ""
