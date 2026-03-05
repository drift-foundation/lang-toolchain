#!/usr/bin/env bash
# Drift deployment verification script.
#
# Usage: tools/deploy/deploy-verify.sh <DEST> <PUBKEY>
#
# Verifies a deployed Drift distribution:
#   1. Ed25519 signature on lib/manifest.json using external trusted pubkey
#   2. SHA-256 hashes of every file listed in manifest against disk
#   3. No unsigned files exist outside the manifest (excluding manifest.json.sig)
#
# DEST may be the top-level deploy directory (containing a `current` symlink)
# or a specific versioned directory.  If DEST/current exists, the symlink
# target is verified.
#
# PUBKEY is the path to a PEM-encoded Ed25519 public key, managed out-of-band.
# No key material is expected inside the deployed tree.
#
# Exits 0 on success, non-zero on any verification failure.
set -euo pipefail

if [[ $# -lt 2 ]]; then
	echo "usage: $0 <DEST> <PUBKEY>" >&2
	echo "" >&2
	echo "  DEST    deploy directory (or parent with 'current' symlink)" >&2
	echo "  PUBKEY  path to trusted Ed25519 public key (PEM)" >&2
	exit 1
fi

DEST="$1"
PUBKEY="$2"

if [[ ! -f "${PUBKEY}" ]]; then
	echo "error: public key not found: ${PUBKEY}" >&2
	exit 1
fi

# Resolve DEST: if it contains `current` symlink, follow it.
# If DEST itself is a staging directory (contains a versioned dir), find it.
if [[ -L "${DEST}/current" ]]; then
	DIST="${DEST}/$(readlink "${DEST}/current")"
elif [[ -f "${DEST}/lib/manifest.json" ]]; then
	DIST="${DEST}"
else
	# Staging layout: DEST contains a single drift-* directory.
	found=""
	for d in "${DEST}"/drift-*; do
		if [[ -d "${d}" && -f "${d}/lib/manifest.json" ]]; then
			found="${d}"
			break
		fi
	done
	if [[ -z "${found}" ]]; then
		echo "error: no manifest.json found under ${DEST}" >&2
		exit 1
	fi
	DIST="${found}"
fi

MANIFEST="${DIST}/lib/manifest.json"
SIGNATURE="${DIST}/lib/manifest.json.sig"

if [[ ! -f "${MANIFEST}" ]]; then
	echo "error: manifest not found: ${MANIFEST}" >&2
	exit 1
fi
if [[ ! -f "${SIGNATURE}" ]]; then
	echo "error: signature not found: ${SIGNATURE}" >&2
	exit 1
fi

echo "[verify] dist:     ${DIST}"
echo "[verify] manifest: ${MANIFEST}"
echo "[verify] pubkey:   ${PUBKEY}"

# ── Step 0: Enforce Ed25519 public key type ──────────────────────────
PUB_KEY_TYPE="$(openssl pkey -pubin -in "${PUBKEY}" -text -noout 2>/dev/null | head -1)"
if [[ "${PUB_KEY_TYPE}" != *"ED25519"* ]]; then
	echo "error: public key must be Ed25519, got: ${PUB_KEY_TYPE}" >&2
	exit 1
fi

# ── Step 1: Verify Ed25519 signature ─────────────────────────────────
echo "[verify] checking signature..."
if ! openssl pkeyutl -verify -pubin -inkey "${PUBKEY}" \
	-rawin -in "${MANIFEST}" -sigfile "${SIGNATURE}" 2>/dev/null; then
	echo "FAIL: manifest signature verification failed" >&2
	exit 1
fi
echo "[verify] signature OK"

# ── Step 2: Verify file hashes ───────────────────────────────────────
echo "[verify] checking file hashes..."
FAIL_COUNT=0

# Extract file_hashes from manifest using portable tools (no jq dependency).
# Format: "relative/path": "sha256hex"
while IFS= read -r line; do
	# Parse "path": "hash" lines.
	path="$(echo "${line}" | sed -n 's/^[[:space:]]*"\(.*\)":[[:space:]]*"\(.*\)".*$/\1/p')"
	expected="$(echo "${line}" | sed -n 's/^[[:space:]]*"\(.*\)":[[:space:]]*"\(.*\)".*$/\2/p')"
	[[ -n "${path}" && -n "${expected}" ]] || continue

	file="${DIST}/${path}"
	if [[ ! -f "${file}" ]]; then
		echo "  MISSING: ${path}" >&2
		FAIL_COUNT=$((FAIL_COUNT + 1))
		continue
	fi

	actual="$(sha256sum "${file}" | cut -d' ' -f1)"
	if [[ "${actual}" != "${expected}" ]]; then
		echo "  MISMATCH: ${path}" >&2
		echo "    expected: ${expected}" >&2
		echo "    actual:   ${actual}" >&2
		FAIL_COUNT=$((FAIL_COUNT + 1))
	fi
done < <(sed -n '/^  "file_hashes"/,/^  }/p' "${MANIFEST}" | grep -E '^\s*"[^"]+": "[0-9a-f]{64}"')

if [[ ${FAIL_COUNT} -gt 0 ]]; then
	echo "FAIL: ${FAIL_COUNT} file hash verification failure(s)" >&2
	exit 1
fi

# Count verified files for reporting.
VERIFIED_COUNT="$(sed -n '/^  "file_hashes"/,/^  }/p' "${MANIFEST}" | grep -cE '^\s*"[^"]+": "[0-9a-f]{64}"' || true)"
echo "[verify] ${VERIFIED_COUNT} file hashes OK"

# ── Step 3: Check for unsigned files ─────────────────────────────────
echo "[verify] checking for unsigned files..."
UNSIGNED_COUNT=0
while IFS= read -r -d '' relpath; do
	# manifest.json and manifest.json.sig are metadata, not in hashes.
	[[ "${relpath}" == "lib/manifest.json" ]] && continue
	[[ "${relpath}" == "lib/manifest.json.sig" ]] && continue
	# Check if this path appears in file_hashes.
	if ! grep -qF "\"${relpath}\"" "${MANIFEST}"; then
		echo "  UNSIGNED: ${relpath}" >&2
		UNSIGNED_COUNT=$((UNSIGNED_COUNT + 1))
	fi
done < <(cd "${DIST}" && find . -type f -print0 | sed -z 's|^\./||' | sort -z)

if [[ ${UNSIGNED_COUNT} -gt 0 ]]; then
	echo "FAIL: ${UNSIGNED_COUNT} file(s) not covered by manifest" >&2
	exit 1
fi

# ── Step 4: Cross-check key fingerprint (informational) ─────────────
KEY_FP="$(openssl pkey -pubin -in "${PUBKEY}" -outform DER 2>/dev/null \
	| sha256sum | cut -d' ' -f1)"
MANIFEST_FP="$(sed -n 's/^.*"sign_key_fingerprint":[[:space:]]*"\([^"]*\)".*/\1/p' "${MANIFEST}")"

if [[ -n "${MANIFEST_FP}" && -n "${KEY_FP}" ]]; then
	if [[ "${KEY_FP}" == "${MANIFEST_FP}" ]]; then
		echo "[verify] key fingerprint matches manifest (${KEY_FP:0:16}...)"
	else
		echo "[verify] WARNING: key fingerprint does not match manifest" >&2
		echo "  pubkey:   ${KEY_FP}" >&2
		echo "  manifest: ${MANIFEST_FP}" >&2
		echo "  (signature was valid — this may indicate key rotation)" >&2
	fi
fi

echo "[verify] PASSED"
