#!/usr/bin/env bash
# Deployed driftc wrapper — self-contained, repo-independent.
#
# This script is installed to <deploy>/bin/driftc and resolves all paths
# relative to its own location so the distribution is fully relocatable.
#
# The stdlib is loaded from a signed DMIR package (lib/stdlib/std.dmp).
# Signature verification uses the bundled core trust store automatically.
#
# Prerequisites: Python 3.10+ with lark, llvmlite, and cryptography packages, clang linker.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIST_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Compiler sources live under lib/compiler/ in the deployed tree.
export PYTHONPATH="${DIST_ROOT}/lib/compiler"

# Prevent CWD from shadowing deployed compiler modules (e.g. if run from
# a repo checkout that also has a lang/ package).
export PYTHONSAFEPATH=1

# Point runtime archive cache at deployed pre-built archives.
export DRIFT_RUNTIME_LIB_CACHE_DIR="${DIST_ROOT}/lib/runtime"

# Locate Python — prefer python3, accept python.
PYTHON="${DRIFT_PYTHON:-}"
if [[ -z "${PYTHON}" ]]; then
	PYTHON="$(command -v python3 2>/dev/null || command -v python 2>/dev/null || true)"
fi
if [[ -z "${PYTHON}" ]]; then
	echo "error: python3 not found; set DRIFT_PYTHON or add python3 to PATH" >&2
	exit 127
fi

# Build argument list: stdlib from signed package, optional user trust store.
DRIFTC_ARGS=(--package-root "${DIST_ROOT}/lib/stdlib")
if [[ -n "${DRIFT_TRUST_STORE:-}" && -f "${DRIFT_TRUST_STORE}" ]]; then
	DRIFTC_ARGS+=(--trust-store "${DRIFT_TRUST_STORE}")
fi

# Use -m with PYTHONSAFEPATH=1 to prevent CWD from appearing in sys.path.
# This ensures Python resolves lang.driftc from PYTHONPATH (deployed tree)
# even when invoked from a directory containing its own lang/ package.
exec "${PYTHON}" -m lang.driftc "${DRIFTC_ARGS[@]}" "$@"
