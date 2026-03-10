# Drift Build & Package Workflow (Draft)

This guide is a practical, end-to-end workflow for new developers:

1. Bootstrap and validate toolchain/compiler infra on a fresh clone.
2. Build a hello-world app (no package dependencies).
3. Build a library package (`.dmp`).
4. Build an app that depends on that package (unsigned local flow).
5. Sign the package and consume it with signature verification enabled.

Commands assume you run from repository root and use the local venv:

```bash
PYTHONPATH=. ./.venv/bin/python3 -m lang.driftc --help
PYTHONPATH=. ./.venv/bin/python3 -m lang.drift --help
```

You can also use the repo-local wrappers (recommended for day-to-day use):

```bash
export DRIFTC="$PWD/bin/driftc"
export DRIFT_TOOL="$PWD/bin/drift"
export DRIFT_TRUST_STORE="$HOME/.config/drift/trust.json"
```

## Trust layers (important)

For most users, use this simple model:

1. Publisher signs upstream packages/toolchain artifacts.
2. User verifies signatures and hashes before use.
3. `deploy` installs a local runnable bundle (for example `~/opt/drift/current`).

Some teams may add internal deploy signing as an optional extra attestation
layer, but early adopters can safely start with publisher-signature verification.

## 1. Bootstrap & validate (fresh clone)

The goal is to confirm your environment can run the full Drift pipeline (parser/checker/MIR/LLVM/codegen/runtime tests) before writing code.

### 1.1 Install prerequisites (Linux)

Install these host packages up front for a normal full build/test workflow:

- Python 3.13+
- `python3-venv`
- `just`
- LLVM/Clang (`clang` on PATH; clang-20 recommended)
- `pkg-config`
- `binutils-gold` (`ld.gold`)
- `libdw-dev`, `libunwind-dev`, `libelf-dev`
- `ripgrep` (`rg`) (needed by stdlib package build/publish recipes)

Then create the venv, install Python deps, and run `just deps-check` as the final wiring check.

### 1.2 Create virtualenv and install Python deps

```bash
python3 -m venv .venv
./.venv/bin/python3 -m pip install -U pip
./.venv/bin/python3 -m pip install -r requirements.txt
```

If you want the repo tasks to use a specific clang explicitly, set:

```bash
export CLANG_BIN=clang-20
export CLANG=clang-20
```

### 1.3 Quick CLI sanity

```bash
$DRIFTC --help
$DRIFT_TOOL --help
```

### 1.4 Verify dependency wiring

```bash
just deps-check
```

If `deps-check` still reports something missing after the package list above, install the missing host dependency and rerun it before moving on.

### 1.5 Run complete compiler + codegen test flow

```bash
just
```

Notes:
- `just` runs the default full staged test path (`lang-test`) after `deps-check`.
- This is the recommended gate before coding in Drift.
- If you want to focus only on codegen/e2e for quick iteration: `just lang-codegen-test`.

### 1.6 Build runtime archives (required for archive-mode driftc)

```bash
just runtime-libs
```

Notes:
- `bin/driftc` defaults to strict runtime archive mode and expects prebuilt archives.
- If archives are missing, compile fails with a runtime archive missing error.
- Use `DRIFT_RUNTIME_LINK_MODE=source` only when explicitly opting into legacy source/object runtime linking.

### 1.7 Create signing identity (package creators)

For signed package publishing, create a local signing key once:

```bash
mkdir -p ~/.config/drift/keys
chmod 700 ~/.config/drift ~/.config/drift/keys
$DRIFT_TOOL keygen --out ~/.config/drift/keys/default.seed --print-pubkey --print-kid
chmod 600 ~/.config/drift/keys/default.seed
```

Set default signing key for recipes/tooling:

```bash
export DRIFT_SIGN_KEY_FILE="$HOME/.config/drift/keys/default.seed"
```

Optional command-based key source (stdout must emit base64 seed):

```bash
export DRIFT_SIGN_KEY_CMD='gpg -d ~/.config/drift/keys/default.seed.pgp'
```

`drift sign` key resolution priority:

1. `--key <path>`
2. `DRIFT_SIGN_KEY_FILE`
3. `DRIFT_SIGN_KEY_CMD`

## 2. Hello app (no dependency package)

Create a simple app:

```bash
mkdir -p sandbox/hello
cat > sandbox/hello/main.drift <<'DRIFT'
module main

fn main() nothrow -> Int {
    return 0;
}
DRIFT
```

Build executable:

```bash
PYTHONPATH=. ./.venv/bin/python3 -m lang.driftc --stdlib-root stdlib sandbox/hello/main.drift -o sandbox/hello/hello_app
```

Run:

```bash
./sandbox/hello/hello_app
```

Notes:
- Entry defaults to `main::main`.
- For executable builds, keep your entry module as `module main`.

## 3. Build a library package (`.dmp`)

Create a tiny library module:

```bash
mkdir -p sandbox/libmath
cat > sandbox/libmath/mathlib.drift <<'DRIFT'
module mathlib

export { add };

pub fn add(a: Int, b: Int) -> Int {
    return a + b;
}
DRIFT
```

Emit package artifact:

```bash
PYTHONPATH=. ./.venv/bin/python3 -m lang.driftc -M sandbox/libmath sandbox/libmath/mathlib.drift --package-id acme.math --package-version 0.1.0 --package-target test-target --emit-package sandbox/libmath/acme.math.dmp --json
```

You should now have:
- `sandbox/libmath/acme.math.dmp`

## 4. Consume the package from an app (unsigned local flow)

Create an app that imports the packaged module:

```bash
mkdir -p sandbox/app_unsigned
cat > sandbox/app_unsigned/main.drift <<'DRIFT'
module main

import mathlib as mathlib;

fn main() nothrow -> Int {
    return try mathlib.add(40, 2) catch { 0 };
}
DRIFT
```

Build by adding package root and allowing unsigned package from local path:

```bash
PYTHONPATH=. ./.venv/bin/python3 -m lang.driftc -M sandbox/app_unsigned --package-root sandbox/libmath --allow-unsigned-from sandbox/libmath --stdlib-root stdlib sandbox/app_unsigned/main.drift -o sandbox/app_unsigned/app_unsigned
```

Run:

```bash
./sandbox/app_unsigned/app_unsigned
echo $?
```

Expected exit code: `42`.

## 5. Sign package and require signatures at compile time

This is the distribution-style flow.

### 5.1 Generate a signing key

```bash
mkdir -p sandbox/keys sandbox/drift
$DRIFT_TOOL keygen --out sandbox/keys/acme.seed --print-pubkey --print-kid
```

Keep output values (`pubkey`, `kid`) for trust-store operations.

### 5.2 Sign the package

```bash
$DRIFT_TOOL sign sandbox/libmath/acme.math.dmp --key sandbox/keys/acme.seed --include-pubkey
```

This produces:
- `sandbox/libmath/acme.math.dmp.sig`

### 5.3 Trust signer for namespace

Import signer directly from package sidecar (namespace auto-derived as `<package_id>.*`):

```bash
$DRIFT_TOOL trust import --trust-store sandbox/drift/trust.json sandbox/libmath/acme.math.dmp.sig
```

By default this prompts for confirmation (`[y/N]`). Use `--yes` for non-interactive automation.

Manual fallback (if sidecar has no embedded pubkey):

```bash
$DRIFT_TOOL trust add-key --trust-store sandbox/drift/trust.json --namespace mathlib.* --pubkey '<BASE64_PUBKEY>'
```

Inspect trust store:

```bash
$DRIFT_TOOL trust list --trust-store sandbox/drift/trust.json --json
```

### 5.4 Build app with signatures required

```bash
$DRIFTC -M sandbox/app_unsigned --package-root sandbox/libmath --trust-store sandbox/drift/trust.json --stdlib-root stdlib sandbox/app_unsigned/main.drift -o sandbox/app_unsigned/app_signed
```

Run:

```bash
./sandbox/app_unsigned/app_signed
echo $?
```

Expected exit code: `42`.

### 5.5 Signed stdlib into local dist repo (creator flow)

Build + sign + publish stdlib package into local repo (`dist/release`):

```bash
just dist-publish-stdlib
```

Behavior:
- Uses `DRIFT_SIGN_KEY_FILE` by default.
- You can override per-run: `just dist-publish-stdlib /path/to/other.seed`.
- The fallback `just dist-publish-stdlib-unsigned` exists for local-only dev.

Inspect local repo index:

```bash
just dist-index
```

## 6. Revoke key (negative check)

Revoke by `kid`:

```bash
$DRIFT_TOOL trust revoke --trust-store sandbox/drift/trust.json --kid '<KID>' --reason 'test revoke'
```

Rebuild should now fail for that package.

## 7. Command checklist

- Build app: `lang.driftc ... -o <exe>`
- Emit package: `lang.driftc ... --emit-package <pkg.dmp> --package-id ... --package-version ... --package-target ...`
- Sign package: `lang.drift sign <pkg.dmp> --key <seed>`
- Trust signer (recommended): `lang.drift trust import <pkg.dmp.sig>`
- Trust signer (manual fallback): `lang.drift trust add-key --namespace <ns> --pubkey <base64>`
- Signature verification is default in `bin/driftc` package mode.
- Opt-out only when needed: `--skip-package-signatures`
- `DRIFT_ASAN=1` is supported in direct `bin/driftc` compile/link mode and injects `-fsanitize=address -g`.
- `DRIFT_MEMCHECK=1` and `DRIFT_MASSIF=1` are runner-only (execution-time) toggles; `bin/driftc` fails fast if they are set.
- Runtime linking defaults to static archive mode (`libdrift_rt.a` variants under `build/runtime_libs/`).
- Archive mode is strict: if archive build/link fails, `driftc` fails (no silent source fallback).
- `driftc` does not auto-build runtime archives in archive mode; archives must already exist.
- Set `DRIFT_RUNTIME_LINK_MODE=source` to force legacy source/object runtime linking.
- Set `DRIFT_RUNTIME_LIB_CACHE_DIR=<path>` to redirect runtime archive artifacts/locks to a caller-writable location.
- Legacy-compatible override: `DRIFT_RUNTIME_BUILD_ROOT=<path>` writes archives under `<path>/runtime_libs/`.

## 8. Common pitfalls

- `module main` is required for default executable entrypoint (`main::main`).
- Imported module ids must match what the package exports.
- If consuming unsigned local packages, pass `--skip-package-signatures` (and optionally `--allow-unsigned-from <dir>` when using raw `lang.driftc`).
- For signed flow, trust store must be configured (`--trust-store` or `DRIFT_TRUST_STORE`).
- `dist-publish-stdlib` requires a signing key (`DRIFT_SIGN_KEY_FILE` or explicit `SIGN_KEY` arg).
- For portability-sensitive environments, if toolchain asks for pointer width, add `--target-word-bits 64`.

## 9. Next expansion ideas

- Multi-package dependency chain (`pkg A -> pkg B -> app`).
- Multiple signatures on one package (multisig acceptance policy).
- CI recipe for publish/sign/trust/consume verification.
