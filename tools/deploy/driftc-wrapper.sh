#!/usr/bin/env bash
# Deployed driftc wrapper — self-contained, repo-independent.
#
# This script is installed to <deploy>/bin/driftc and resolves all paths
# relative to its own location so the distribution is fully relocatable.
#
# The stdlib is loaded from a signed DMIR package (lib/stdlib/std.dmp).
# Signature verification uses the bundled core trust store automatically.
#
# Prerequisites: Python 3.10+ and clang linker.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIST_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Compiler sources and vendored Python deps live inside the deployed tree.
export PYTHONPATH="${DIST_ROOT}/lib/python_vendor:${DIST_ROOT}/lib/compiler"

# Prevent CWD from shadowing deployed compiler modules (e.g. if run from
# a repo checkout that also has a lang/ package).
export PYTHONSAFEPATH=1

# Runtime archive resolution: point driftc directly at this deployment
# tree's `lib/runtime/`.  The .a files there are pre-built, signed
# alongside the rest of the toolchain, and serve as the single source
# of truth for this deployment.  `ld.gold` opens the archive read-only,
# so a 0444 install tree is fine — no copy, no chmod, no user-local
# cache.
#
# No `~/.cache/drift/runtime/`: a process-wide writable cache that
# survives toolchain upgrades is a silent-Frankenstein hazard.  Each
# deployment is self-contained.  An operator-provided env var still
# wins (CI scratch dir under /tmp); the default just points at the
# install tree's read-only artifact.
if [[ -z "${DRIFT_RUNTIME_LIB_CACHE_DIR:-}" ]]; then
	if [[ -d "${DIST_ROOT}/lib/runtime" ]]; then
		export DRIFT_RUNTIME_LIB_CACHE_DIR="${DIST_ROOT}/lib/runtime"
	fi
fi

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
# Read stdlib dep spec from deploy-time metadata (std@<version>).
STDLIB_DEP_FILE="${DIST_ROOT}/lib/stdlib/stdlib_dep.txt"
DRIFTC_ARGS=(--package-root "${DIST_ROOT}/lib/stdlib")
if [[ -f "${STDLIB_DEP_FILE}" ]]; then
	DRIFTC_ARGS+=(--dep "$(cat "${STDLIB_DEP_FILE}")")
fi
# Exists-before-injecting -- matches tools/deploy/pex_entry.py and
# tools/drift_deploy/drift_deploy.py::_resolve_trust_store.
#   - DRIFT_TRUST_STORE set + file exists -> forward.
#   - DRIFT_TRUST_STORE set + file missing -> fail loud.  The env
#     var is explicit intent; silently dropping it masked the
#     cert-host net-tls staging failure.
#   - DRIFT_TRUST_STORE unset -> do nothing; driftc picks up the
#     ~/.config/drift/trust.json user-trust layer on its own
#     (gated on existence in lang/driftc/driftc.py).
if [[ -n "${DRIFT_TRUST_STORE:-}" ]]; then
	if [[ ! -f "${DRIFT_TRUST_STORE}" ]]; then
		echo "error: \$DRIFT_TRUST_STORE points at a path that does not exist: ${DRIFT_TRUST_STORE}" >&2
		echo "hint: unset DRIFT_TRUST_STORE to let driftc fall through to its default user-trust layer, or repair the path." >&2
		exit 1
	fi
	DRIFTC_ARGS+=(--trust-store "${DRIFT_TRUST_STORE}")
fi

# Run in isolated site-disabled mode so the deployed toolchain does not
# depend on ambient Python packages from the caller environment.
exec "${PYTHON}" -S -m lang.driftc "${DRIFTC_ARGS[@]}" "$@"
