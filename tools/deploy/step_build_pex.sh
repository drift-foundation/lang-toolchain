#!/usr/bin/env bash
# Deploy step: build PEX --scie eager executable for driftc.
#
# Inputs (env):
#   REPO_ROOT       — repository root
#   DIST            — staged distribution directory
#
# Produces ${DIST}/bin/driftc as a self-contained PEX --scie eager binary
# that embeds CPython and all third-party Python dependencies.
#
# The compiler sources are NOT included in the PEX; they remain in
# lib/compiler/ and are added to sys.path by the entry point at runtime.
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
		echo "${pkg}"
	fi
}

LARK_REQ="$(_read_req_version lark)"
LLVMLITE_REQ="$(_read_req_version llvmlite)"
CRYPTO_REQ="$(_read_req_version cryptography)"

# ── Detect Python version for scie ──────────────────────────────────
PYTHON_VERSION="$("${VENV}/bin/python3" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"

# ── Build PEX entry point source directory ──────────────────────────
ENTRY_DIR="$(mktemp -d)"
trap 'rm -rf "${ENTRY_DIR}"' RETURN
cp "${REPO_ROOT}/tools/deploy/pex_entry.py" "${ENTRY_DIR}/"

# ── Build PEX ───────────────────────────────────────────────────────
mkdir -p "${DIST}/bin"

echo "[deploy] building PEX --scie eager executable (deps: ${LARK_REQ}, ${LLVMLITE_REQ}, ${CRYPTO_REQ})..."
"${PEX_CMD}" \
	"${LARK_REQ}" \
	"${LLVMLITE_REQ}" \
	"${CRYPTO_REQ}" \
	-D "${ENTRY_DIR}" \
	-e pex_entry:main \
	--scie eager \
	--scie-python-version "${PYTHON_VERSION}" \
	--python "${VENV}/bin/python3" \
	-o "${DIST}/bin/driftc"

chmod +x "${DIST}/bin/driftc"
echo "[deploy] PEX executable built: ${DIST}/bin/driftc"
