#!/usr/bin/env bash
# Drift distribution deploy script.
#
# Usage: tools/deploy/deploy.sh <DEST>
#
# Builds a versioned, self-contained Drift distribution under DEST:
#   DEST/drift-<VERSION>+abi<ABI>/  (bin, lib, doc, examples)
#   DEST/current -> drift-<VERSION>+abi<ABI>  (atomic symlink)
#
# The deploy is staged in a temp directory, validated via smoke test using
# only deployed paths, signed, self-verified, then moved into place atomically.
#
# Signing is mandatory.  Provide one of:
#   DRIFT_DEPLOY_SIGN_KEY=/path/to/ed25519-private.pem
#   DRIFT_DEPLOY_SIGNER="command that reads stdin, writes sig to stdout"
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VERIFY_SCRIPT="${REPO_ROOT}/tools/deploy/deploy-verify.sh"

if [[ $# -lt 1 ]]; then
	echo "usage: $0 <DEST>" >&2
	echo "" >&2
	echo "env: DRIFT_DEPLOY_SIGN_KEY=/path/to/ed25519-private.pem  (required)" >&2
	echo "     DRIFT_DEPLOY_SIGNER=\"cmd\"  (alternative: stdin=data, stdout=sig)" >&2
	exit 1
fi

SIGN_KEY="${DRIFT_DEPLOY_SIGN_KEY:-}"
SIGNER="${DRIFT_DEPLOY_SIGNER:-}"

if [[ -z "${SIGN_KEY}" && -z "${SIGNER}" ]]; then
	echo "error: signing is required." >&2
	echo "  set DRIFT_DEPLOY_SIGN_KEY=/path/to/ed25519-private.pem" >&2
	echo "  or  DRIFT_DEPLOY_SIGNER=\"command\"" >&2
	exit 1
fi
if [[ -n "${SIGN_KEY}" && ! -f "${SIGN_KEY}" ]]; then
	echo "error: sign key not found: ${SIGN_KEY}" >&2
	exit 1
fi
if [[ -n "${SIGNER}" && -z "${DRIFT_DEPLOY_VERIFY_PUBKEY:-}" ]]; then
	echo "error: DRIFT_DEPLOY_VERIFY_PUBKEY is required when using DRIFT_DEPLOY_SIGNER" >&2
	exit 1
fi

DEST="$(cd "$(dirname "$1")" 2>/dev/null && pwd)/$(basename "$1")" || {
	mkdir -p "$1"
	DEST="$(cd "$1" && pwd)"
}

# ── Read version constants ────────────────────────────────────────────
DRIFTC_VERSION="$(cd "${REPO_ROOT}" && PYTHONPATH=. ./.venv/bin/python3 -c \
	"from lang.driftc.driftc_versions import DRIFTC_VERSION; print(DRIFTC_VERSION)")"
ABI_VERSION="$(cd "${REPO_ROOT}" && PYTHONPATH=. ./.venv/bin/python3 -c \
	"from lang.driftc.driftc_versions import DRIFT_RT_ABI_VERSION; print(DRIFT_RT_ABI_VERSION)")"
GIT_COMMIT="$(git -C "${REPO_ROOT}" rev-parse --short HEAD 2>/dev/null || echo "unknown")"
GIT_COMMIT_FULL="$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || echo "unknown")"
BUILD_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
HOST_PLATFORM="$(uname -s | tr '[:upper:]' '[:lower:]')"
HOST_ARCH="$(uname -m)"

VERSION_DIR="drift-${DRIFTC_VERSION}+abi${ABI_VERSION}"

echo "[deploy] version:  ${DRIFTC_VERSION}"
echo "[deploy] abi:      ${ABI_VERSION}"
echo "[deploy] commit:   ${GIT_COMMIT}"
echo "[deploy] dest:     ${DEST}/${VERSION_DIR}"

# ── Validate and fingerprint signing key ──────────────────────────────
SIGN_KEY_FINGERPRINT=""
if [[ -n "${SIGN_KEY}" ]]; then
	# Enforce Ed25519 key type — reject RSA, EC, etc.
	KEY_TYPE="$(openssl pkey -in "${SIGN_KEY}" -text -noout 2>/dev/null | head -1)"
	if [[ "${KEY_TYPE}" != *"ED25519"* ]]; then
		echo "error: signing key must be Ed25519, got: ${KEY_TYPE}" >&2
		exit 1
	fi
	# SHA-256 of the DER-encoded public key, hex-encoded.
	SIGN_KEY_FINGERPRINT="$(openssl pkey -in "${SIGN_KEY}" -pubout -outform DER 2>/dev/null \
		| sha256sum | cut -d' ' -f1)"
	echo "[deploy] sign key: ${SIGN_KEY_FINGERPRINT:0:16}..."
fi

# ── Detect clang ──────────────────────────────────────────────────────
CLANG="$(command -v clang-15 2>/dev/null || command -v clang 2>/dev/null || true)"
if [[ -z "${CLANG}" ]]; then
	echo "error: clang not found" >&2
	exit 1
fi

# ── Build runtime archives (in-repo, all variants) ───────────────────
echo "[deploy] building runtime archives..."
(cd "${REPO_ROOT}" && DRIFT_RUNTIME_CLANG="${CLANG}" PYTHONPATH=. ./.venv/bin/python3 -c "
from pathlib import Path; import os
from lang.language_runtime import build_runtime_archive
root = Path('.').resolve()
clang = os.environ['DRIFT_RUNTIME_CLANG']
for v in ('default','debug','asan','alloc_track','optimized'):
    build_runtime_archive(root, clang=clang, variant=v)
") >/dev/null

# ── Stage into temp directory ─────────────────────────────────────────
mkdir -p "${DEST}"
STAGE="$(mktemp -d "${DEST}/.deploy-staging.XXXXXX")"
trap 'rm -rf "${STAGE}"' EXIT

DIST="${STAGE}/${VERSION_DIR}"
mkdir -p "${DIST}/bin" "${DIST}/lib/runtime" "${DIST}/lib/compiler" "${DIST}/lib/stdlib" \
         "${DIST}/doc" "${DIST}/examples"

# bin/ — wrapper + helper
cp "${REPO_ROOT}/tools/deploy/driftc-wrapper.sh" "${DIST}/bin/driftc"
chmod +x "${DIST}/bin/driftc"

# lib/compiler/ — compiler Python sources (lang/ tree)
for pkg in lang/driftc lang/codegen lang/compiler_infra lang/language_runtime; do
	src="${REPO_ROOT}/${pkg}"
	dst="${DIST}/lib/compiler/${pkg}"
	if [[ -d "${src}" ]]; then
		mkdir -p "${dst}"
		# Copy .py and data files (.lark grammars) preserving directory structure.
		(cd "${REPO_ROOT}" && find "${pkg}" \( -name '*.py' -o -name '*.lark' \) -print0 | while IFS= read -r -d '' f; do
			target="${DIST}/lib/compiler/${f}"
			mkdir -p "$(dirname "${target}")"
			cp "${f}" "${target}"
		done)
	fi
done
touch "${DIST}/lib/compiler/lang/__init__.py"
(cd "${REPO_ROOT}" && find lang/language_runtime lang/compiler_infra \
	\( -name '*.c' -o -name '*.h' -o -name '*.S' \) -print0 | \
	while IFS= read -r -d '' f; do
		target="${DIST}/lib/compiler/${f}"
		mkdir -p "$(dirname "${target}")"
		cp "${f}" "${target}"
	done)

# lib/runtime/ — pre-built archives for all variants
for variant in default debug asan alloc_track optimized; do
	src="${REPO_ROOT}/build/runtime_libs/${variant}/libdrift_rt.a"
	if [[ -f "${src}" ]]; then
		mkdir -p "${DIST}/lib/runtime/${variant}"
		cp "${src}" "${DIST}/lib/runtime/${variant}/libdrift_rt.a"
	else
		echo "warning: runtime archive for variant '${variant}' not found, skipping" >&2
	fi
done

# lib/stdlib/ — all .drift files from stdlib/
(cd "${REPO_ROOT}" && find stdlib -name '*.drift' -print0 | while IFS= read -r -d '' f; do
	target="${DIST}/lib/stdlib/${f}"
	mkdir -p "$(dirname "${target}")"
	cp "${f}" "${target}"
done)

# Remove build artifacts that leak from runtime archive cache.
find "${DIST}" -name '.build.lock' -delete

# ── doc/ ──────────────────────────────────────────────────────────────
cat > "${DIST}/doc/README.md" <<'DOC_EOF'
# Drift Distribution

## Prerequisites

The Drift compiler requires these host tools:

| Dependency | Version | Purpose |
|------------|---------|---------|
| Python 3 | 3.10+ | Compiler runtime |
| lark | any | Parser library (`pip install lark`) |
| llvmlite | 0.41+ | LLVM IR generation (`pip install llvmlite`) |
| clang | 15+ | Linker / native codegen |

Verify your environment:

```bash
python3 -c "import lark, llvmlite; print('ok')"
clang --version   # or clang-15 --version
```

Set `DRIFT_PYTHON` to override which Python interpreter the compiler uses.

## Quick start

```bash
export PATH="<deploy-root>/current/bin:$PATH"
driftc my_program.drift -o my_program
./my_program
```

## Using the compiler

`bin/driftc` is a wrapper that locates the stdlib and runtime archives
relative to its own path.  No repo checkout or PYTHONPATH setup is needed —
only the prerequisites above.

### Flags

| Flag | Purpose |
|------|---------|
| `-o <path>` | Output binary path |
| `--optimized` | Build with -O2 and optimized runtime |
| `-g` / `--debug-info` | Emit DWARF debug info |
| `--entry <mod>::<fn>` | Custom entry point (default: `main::main`) |
| `--json` | Machine-readable diagnostics |

### Environment

| Variable | Purpose |
|----------|---------|
| `DRIFT_PYTHON` | Override Python interpreter (default: `python3`) |
| `DRIFT_ASAN` | Set to `1` to link with AddressSanitizer runtime |

## ABI compatibility

Each distribution is built against a specific runtime ABI version
(see `lib/manifest.json`).  Binaries compiled with one ABI version
cannot link against a runtime built for a different version — the
linker will fail with an unresolved `__drift_rt_abi_version_N` symbol.

The `current` symlink always points to the latest deployed version.
Older versions remain available under their versioned directory names
and continue to work independently.

## Switching versions

```bash
# Deploy creates: <dest>/drift-<version>+abi<N>/
# And updates:    <dest>/current -> drift-<version>+abi<N>

# To pin to an older version:
export PATH="<dest>/drift-0.27.0-dev+abi3/bin:$PATH"
```

## Verifying a deployment

Every deploy is signed.  Verify with an external trusted public key:

```bash
just deploy-verify DEST=/path/to/deploy PUBKEY=/path/to/trusted.pub
```

Or directly:

```bash
tools/deploy/deploy-verify.sh /path/to/deploy /path/to/trusted.pub
```

This checks:
1. Ed25519 signature on `lib/manifest.json` using the trusted public key
2. SHA-256 hashes of every file listed in the manifest against disk
3. No unsigned files exist outside the manifest

## Create a deploy signing key (first-time setup)

If you do not already have a deploy signing key, create one:

```bash
mkdir -p ~/.config/drift/keys
chmod 700 ~/.config/drift ~/.config/drift/keys

openssl genpkey -algorithm Ed25519 -out ~/.config/drift/keys/deploy-ed25519.pem
chmod 600 ~/.config/drift/keys/deploy-ed25519.pem

openssl pkey -in ~/.config/drift/keys/deploy-ed25519.pem -pubout \
  -out ~/.config/drift/keys/deploy-ed25519.pub.pem
chmod 644 ~/.config/drift/keys/deploy-ed25519.pub.pem
```

Use the private key for deployment signing:

```bash
export DRIFT_DEPLOY_SIGN_KEY="$HOME/.config/drift/keys/deploy-ed25519.pem"
just deploy /path/to/deploy-root
```

Consumers verify with the trusted public key (managed out-of-band):

```bash
just deploy-verify DEST=/path/to/deploy-root PUBKEY="$HOME/.config/drift/keys/deploy-ed25519.pub.pem"
```

## Trust layers

Drift deployments can have two independent signature layers:

1. Upstream publisher signature (source authenticity)
   - Proves the toolchain/package received from upstream is authentic.
2. Internal deploy signature (installer/deploy attestation)
   - Proves the exact deployed bundle your team uses (`<dest>/current`) is what
     your internal deploy process approved and published.

These layers are complementary, not redundant.  Upstream signatures establish
origin authenticity; deploy signatures establish integrity/provenance of your
internal distribution endpoint.

## Actors and trust boundaries

Use this simple 3-actor model:

1. Publisher
   - Produces and signs upstream artifacts (packages/toolchain releases).
2. Distributor
   - Downloads/approves upstream artifacts, builds internal deployment bundles,
     and signs the deploy manifest for internal distribution.
3. User (consumer)
   - Verifies distributor signatures before activating a deployment, then uses
     the deployed compiler/runtime for day-to-day builds.

In other words: publisher proves origin, distributor proves internal deployment
integrity, user verifies before use.

Trusted keys are managed out-of-band.  The `sign_key_fingerprint` field
in `manifest.json` is informational (SHA-256 of the DER public key) —
it is NOT a trust source, but can be used for operator cross-checks.

No key material (public or private) is included in the deployed tree.
DOC_EOF

# ── examples/ ─────────────────────────────────────────────────────────
cat > "${DIST}/examples/hello.drift" <<'EXAMPLE_EOF'
module main

import std.console as console;

pub fn main() nothrow -> Int {
	console.println("hello, drift!");
	return 0;
}
EXAMPLE_EOF

cat > "${DIST}/examples/README.md" <<'EXREADME_EOF'
# Examples

## hello.drift

Compile and run:

```bash
driftc examples/hello.drift -o /tmp/hello
/tmp/hello
```

Expected output: `hello, drift!` with exit code 0.
EXREADME_EOF

# ── Smoke test ────────────────────────────────────────────────────────
# Run BEFORE manifest/signing so smoke artifacts don't pollute hashes.
# The smoke test uses the deployed wrapper with the SAME Python that end-users
# will use (no repo venv override).  This catches missing deps early.
echo "[deploy] running smoke test..."
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
for dep in lark llvmlite; do
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

"${DIST}/bin/driftc" "${SMOKE_SRC}" -o "${SMOKE_BIN}" 2>&1 | tail -3
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

# ── Manifest with file hashes ────────────────────────────────────────
# Compute SHA-256 of every deployed file (relative to DIST root).
# This runs AFTER smoke test and artifact cleanup so hashes are final.
echo "[deploy] computing file hashes..."
FILE_HASHES_JSON="{"
hash_first=1
while IFS= read -r -d '' relpath; do
	hash="$(sha256sum "${DIST}/${relpath}" | cut -d' ' -f1)"
	[[ ${hash_first} -eq 1 ]] || FILE_HASHES_JSON+=","
	hash_first=0
	FILE_HASHES_JSON+=$'\n'"    \"${relpath}\": \"${hash}\""
done < <(cd "${DIST}" && find . -type f -print0 | sed -z 's|^\./||' | sort -z)
FILE_HASHES_JSON+=$'\n'"  }"

VARIANTS_JSON="["
first=1
for variant in default debug asan alloc_track optimized; do
	archive="${DIST}/lib/runtime/${variant}/libdrift_rt.a"
	if [[ -f "${archive}" ]]; then
		[[ ${first} -eq 1 ]] || VARIANTS_JSON+=","
		first=0
		VARIANTS_JSON+="\"${variant}\""
	fi
done
VARIANTS_JSON+="]"

cat > "${DIST}/lib/manifest.json" <<MANIFEST_EOF
{
  "driftc_version": "${DRIFTC_VERSION}",
  "runtime_abi_version": ${ABI_VERSION},
  "git_commit": "${GIT_COMMIT_FULL}",
  "build_utc": "${BUILD_UTC}",
  "host_platform": "${HOST_PLATFORM}",
  "host_arch": "${HOST_ARCH}",
  "sign_key_fingerprint": "${SIGN_KEY_FINGERPRINT}",
  "runtime_variants": ${VARIANTS_JSON},
  "file_hashes": ${FILE_HASHES_JSON}
}
MANIFEST_EOF

# ── Sign manifest ────────────────────────────────────────────────────
echo "[deploy] signing manifest..."
if [[ -n "${SIGN_KEY}" ]]; then
	openssl pkeyutl -sign -inkey "${SIGN_KEY}" \
		-rawin -in "${DIST}/lib/manifest.json" \
		-out "${DIST}/lib/manifest.json.sig"
else
	${SIGNER} < "${DIST}/lib/manifest.json" > "${DIST}/lib/manifest.json.sig"
fi

if [[ ! -s "${DIST}/lib/manifest.json.sig" ]]; then
	echo "error: signing produced empty signature" >&2
	exit 1
fi
echo "[deploy] signature: lib/manifest.json.sig ($(wc -c < "${DIST}/lib/manifest.json.sig") bytes)"

# ── Self-verify staged artifact ──────────────────────────────────────
# Extract pubkey from sign key for self-verification (pubkey is NOT deployed).
echo "[deploy] self-verifying staged artifact..."
SELF_VERIFY_PUBKEY="${STAGE}/.verify-pubkey.pem"
if [[ -n "${SIGN_KEY}" ]]; then
	openssl pkey -in "${SIGN_KEY}" -pubout -out "${SELF_VERIFY_PUBKEY}" 2>/dev/null
else
	# Custom signer: require DRIFT_DEPLOY_VERIFY_PUBKEY for self-verify.
	if [[ -z "${DRIFT_DEPLOY_VERIFY_PUBKEY:-}" ]]; then
		echo "error: DRIFT_DEPLOY_VERIFY_PUBKEY is required when using DRIFT_DEPLOY_SIGNER" >&2
		exit 1
	fi
	cp "${DRIFT_DEPLOY_VERIFY_PUBKEY}" "${SELF_VERIFY_PUBKEY}"
fi

if [[ -n "${SELF_VERIFY_PUBKEY}" ]]; then
	"${VERIFY_SCRIPT}" "${STAGE}" "${SELF_VERIFY_PUBKEY}"
	echo "[deploy] self-verify passed"
	rm -f "${SELF_VERIFY_PUBKEY}"
fi

# ── Atomic publish ────────────────────────────────────────────────────
mkdir -p "${DEST}"
FINAL="${DEST}/${VERSION_DIR}"

# If same version already exists, remove it (redeploy).
if [[ -d "${FINAL}" ]]; then
	echo "[deploy] replacing existing ${VERSION_DIR}"
	rm -rf "${FINAL}"
fi

mv "${DIST}" "${FINAL}"

# Atomic symlink switch: create temp link, then rename over current.
TMPLINK="${DEST}/.current.tmp.$$"
ln -snf "${VERSION_DIR}" "${TMPLINK}"
mv -Tf "${TMPLINK}" "${DEST}/current" 2>/dev/null || \
	mv -f "${TMPLINK}" "${DEST}/current"

# Clean up staging (trap will handle if we exit early).
rm -rf "${STAGE}"
trap - EXIT

echo "[deploy] published: ${FINAL}"
echo "[deploy] current -> ${VERSION_DIR}"
echo ""
echo "  export PATH=\"${DEST}/current/bin:\$PATH\""
echo ""
