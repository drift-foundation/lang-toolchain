#!/usr/bin/env bash
# Deploy step: atomically publish staged distribution.
#
# Inputs (env):
#   DEST            — top-level deploy directory
#   VERSION_DIR     — versioned directory name (e.g. drift-0.27.3+abi4)
#   DIST            — staged distribution directory to publish
#   STAGE           — staging scratch directory (cleaned after publish)
#
# Publishes ${DIST} as ${DEST}/${VERSION_DIR} and atomically switches
# the ${DEST}/current symlink.  Safe same-version replacement with
# rollback on failure.
set -euo pipefail

: "${DEST:?}"
: "${VERSION_DIR:?}"
: "${DIST:?}"
: "${STAGE:?}"

# ── Generate manifest ────────────────────────────────────────────────
# Requires: DRIFTC_VERSION, ABI_VERSION, GIT_COMMIT_FULL, BUILD_UTC,
#           HOST_PLATFORM, HOST_ARCH (all set by orchestrator).
: "${DRIFTC_VERSION:?}"
: "${ABI_VERSION:?}"

RUNTIME_VARIANTS=""
for variant in default debug asan alloc_track optimized; do
	if [[ -f "${DIST}/lib/runtime/${variant}/libdrift_rt.a" ]]; then
		if [[ -n "${RUNTIME_VARIANTS}" ]]; then
			RUNTIME_VARIANTS="${RUNTIME_VARIANTS}, \"${variant}\""
		else
			RUNTIME_VARIANTS="\"${variant}\""
		fi
	fi
done

cat > "${DIST}/lib/manifest.json" <<MANIFEST_EOF
{
  "driftc_version": "${DRIFTC_VERSION}",
  "runtime_abi_version": ${ABI_VERSION},
  "git_commit": "${GIT_COMMIT_FULL:-unknown}",
  "build_utc": "${BUILD_UTC:-unknown}",
  "host_platform": "${HOST_PLATFORM:-unknown}",
  "host_arch": "${HOST_ARCH:-unknown}",
  "entrypoint": "pex-scie-eager",
  "runtime_variants": [${RUNTIME_VARIANTS}]
}
MANIFEST_EOF

# ── Atomic publish ────────────────────────────────────────────────────
mkdir -p "${DEST}"
FINAL="${DEST}/${VERSION_DIR}"

if [[ -d "${FINAL}" ]]; then
	BACKUP="${DEST}/.${VERSION_DIR}.old.$$"
	echo "[deploy] replacing existing ${VERSION_DIR}"
	mv "${FINAL}" "${BACKUP}"
	if mv "${DIST}" "${FINAL}"; then
		rm -rf "${BACKUP}"
	else
		mv "${BACKUP}" "${FINAL}" || true
		echo "error: failed to publish ${VERSION_DIR}" >&2
		exit 1
	fi
else
	mv "${DIST}" "${FINAL}"
fi

# Atomic symlink switch: create temp link, then rename over current.
TMPLINK="${DEST}/.current.tmp.$$"
ln -snf "${VERSION_DIR}" "${TMPLINK}"
mv -Tf "${TMPLINK}" "${DEST}/current" 2>/dev/null || \
	mv -f "${TMPLINK}" "${DEST}/current"

# Clean up staging.
rm -rf "${STAGE}"

echo "[deploy] published: ${FINAL}"
echo "[deploy] current -> ${VERSION_DIR}"
