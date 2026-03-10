#!/usr/bin/env bash
# Deploy step: bundle compiler, runtime, wrapper, docs, examples into staged tree.
#
# Inputs (env):
#   REPO_ROOT       — repository root
#   DIST            — staged distribution directory (e.g. .../drift-0.27.3+abi4)
#   CLANG           — path to clang binary
#
# Creates the staged layout under DIST with everything except stdlib package.
set -euo pipefail

: "${REPO_ROOT:?}"
: "${DIST:?}"
: "${CLANG:?}"

mkdir -p "${DIST}/bin" "${DIST}/lib/runtime" "${DIST}/lib/compiler" "${DIST}/lib/stdlib" \
         "${DIST}/lib/python_vendor" "${DIST}/doc" "${DIST}/examples"

# bin/ — wrapper
cp "${REPO_ROOT}/tools/deploy/driftc-wrapper.sh" "${DIST}/bin/driftc"
chmod +x "${DIST}/bin/driftc"

# lib/compiler/ — compiler Python sources (lang/ tree)
for pkg in lang/driftc lang/codegen lang/compiler_infra lang/language_runtime; do
	src="${REPO_ROOT}/${pkg}"
	dst="${DIST}/lib/compiler/${pkg}"
	if [[ -d "${src}" ]]; then
		mkdir -p "${dst}"
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

# lib/python_vendor/ — bundled third-party Python runtime deps
(cd "${REPO_ROOT}" && PYTHONPATH=. ./.venv/bin/python3 tools/deploy/vendor_python_deps.py \
	--dest "${DIST}/lib/python_vendor" \
	lark llvmlite cryptography)

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

# Remove build artifacts that leak from runtime archive cache.
find "${DIST}" -name '.build.lock' -delete

# doc/
cat > "${DIST}/doc/README.md" <<'DOC_EOF'
# Drift Distribution

## Prerequisites

The Drift compiler requires these host tools:

| Dependency | Version | Purpose |
|------------|---------|---------|
| Python 3 | 3.10+ | Compiler runtime |
| clang | 15+ | Linker / native codegen |

Verify your environment:

```bash
python3 --version
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

`bin/driftc` is a wrapper that locates the stdlib package, vendored Python
dependencies, and runtime archives relative to its own path. No repo checkout,
ambient `pip install`, or PYTHONPATH setup is needed — only the prerequisites
above.

### Stdlib integrity

The standard library is shipped as a signed DMIR package (`lib/stdlib/std.dmp`)
with a detached signature sidecar (`lib/stdlib/std.dmp.sig`).  The compiler
verifies the signature against the bundled core trust store at compile time.
Tampered or unsigned stdlib packages are rejected.

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
| `DRIFT_TRUST_STORE` | Path to trust store JSON for user/third-party packages |

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

## Deploy semantics

`deploy.sh` orchestrates four step scripts:

1. `step_bundle.sh` — copy compiler, runtime, wrapper, docs into staged tree
2. `step_stdlib_pkg.sh` — build, sign, and install stdlib package + core trust store
3. `step_smoke.sh` — compile and run smoke test using only deployed paths
4. `step_publish.sh` — atomically publish staged tree and switch `current` symlink

If any step fails, deploy exits non-zero and does not publish a partial install.
DOC_EOF

# examples/
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

echo "[deploy] bundle complete"
