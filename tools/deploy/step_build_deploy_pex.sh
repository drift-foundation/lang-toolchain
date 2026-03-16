#!/usr/bin/env bash
# Deploy step: build PEX --scie eager executable for drift-deploy.
#
# Inputs (env):
#   REPO_ROOT       — repository root
#   DIST            — staged distribution directory
#
# Produces ${DIST}/bin/drift-deploy as a self-contained PEX --scie eager
# binary that embeds CPython and all required Python dependencies.
#
# The tools.drift_deploy package is baked into the PEX.  Deferred imports
# from lang.* (sign, crypto, zdmp, dmir_pkg_v0) are resolved at runtime
# via lib/compiler/ on sys.path (set up by the entry point).
set -euo pipefail

: "${REPO_ROOT:?}"
: "${DIST:?}"

VENV="${REPO_ROOT}/.venv"
PEX_CMD="${VENV}/bin/pex"

# ── Require pex ──────────────────────────────────────────────────────
if [[ ! -x "${PEX_CMD}" ]]; then
	echo "error: pex not found at ${PEX_CMD}" >&2
	echo "  install into the project venv:" >&2
	echo "    ./.venv/bin/pip install pex" >&2
	exit 1
fi

# ── Read pinned dependency versions from requirements.txt ────────────
_read_req_version() {
	local pkg="$1"
	local req_file="${REPO_ROOT}/requirements.txt"
	if [[ ! -f "${req_file}" ]]; then
		echo "${pkg}"
		return
	fi
	local line
	line="$(grep -i "^${pkg}==" "${req_file}" | head -1)" || true
	if [[ -n "${line}" ]]; then
		echo "${line}"
	else
		# Fall back to >= constraint if present.
		line="$(grep -i "^${pkg}>=" "${req_file}" | head -1)" || true
		if [[ -n "${line}" ]]; then
			echo "${line}"
		else
			echo "${pkg}"
		fi
	fi
}

CRYPTO_REQ="$(_read_req_version cryptography)"
ZSTD_REQ="$(_read_req_version zstandard)"

# ── Detect Python version for scie ──────────────────────────────────
PYTHON_VERSION="$("${VENV}/bin/python3" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"

# ── Stage source directories for PEX ────────────────────────────────
# PEX needs the tools.drift_deploy package and the entry point.
ENTRY_DIR="$(mktemp -d)"
trap 'rm -rf "${ENTRY_DIR}"' RETURN

# Entry point.
cp "${REPO_ROOT}/tools/deploy/deploy_pex_entry.py" "${ENTRY_DIR}/"

# tools.drift_deploy package (exclude tests).
mkdir -p "${ENTRY_DIR}/tools/drift_deploy"
cp "${REPO_ROOT}/tools/__init__.py" "${ENTRY_DIR}/tools/" 2>/dev/null || \
	touch "${ENTRY_DIR}/tools/__init__.py"
for f in "${REPO_ROOT}/tools/drift_deploy/"*.py; do
	fname="$(basename "${f}")"
	# Skip test files — not needed at runtime.
	case "${fname}" in
		test_*.py) continue ;;
	esac
	cp "${f}" "${ENTRY_DIR}/tools/drift_deploy/"
done

# ── Build PEX ───────────────────────────────────────────────────────
mkdir -p "${DIST}/bin"

echo "[deploy] building drift-deploy PEX --scie eager executable (deps: ${CRYPTO_REQ}, ${ZSTD_REQ})..."
"${PEX_CMD}" \
	"${CRYPTO_REQ}" \
	"${ZSTD_REQ}" \
	-D "${ENTRY_DIR}" \
	-e deploy_pex_entry:main \
	--scie eager \
	--scie-python-version "${PYTHON_VERSION}" \
	--python "${VENV}/bin/python3" \
	-o "${DIST}/bin/drift-deploy"

chmod +x "${DIST}/bin/drift-deploy"
echo "[deploy] drift-deploy PEX executable built: ${DIST}/bin/drift-deploy"
