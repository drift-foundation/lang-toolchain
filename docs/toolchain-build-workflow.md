# Drift Build & Package Workflow (Draft)

This guide is a practical, end-to-end workflow for new developers:

1. Bootstrap and validate toolchain/compiler infra on a fresh clone.
2. Build a hello-world app (no package dependencies).
3. Build a library package (`.dmp`).
4. Build an app that depends on that package (unsigned local flow).
5. Set up publishing identity, sign the package, and consume it with signature verification.
6. Prepare and deploy packages.

Commands assume you run from repository root and use the local venv:

```bash
PYTHONPATH=. ./.venv/bin/python3 -m lang.driftc --help
PYTHONPATH=. ./.venv/bin/python3 -m lang.drift --help
```

You can also use the repo-local wrappers (recommended for day-to-day use):

```bash
export DRIFTC="$PWD/bin/driftc"
export DRIFT_TOOL="$PWD/bin/drift"
```

## Trust layers (important)

For most users, use this simple model:

1. Publisher runs `drift init` to set up a signing key and public author profile.
2. `drift deploy` publishes the package and a deployed `.author-profile` inside the versioned artifact directory.
3. The deployed signature binds both the package bytes and the deployed `.author-profile`.
4. Consumer obtains that deployed `.author-profile` and runs `drift trust <file>.author-profile` to trust it.
5. `driftc` verifies package signatures against the trust store at compile time.

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

For **deploy** (`just deploy`), you also need `pex` in the project venv:

```bash
./.venv/bin/pip install pex
```

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

### 1.7 Set up publishing identity (package creators)

Run the interactive setup:

```bash
$DRIFT_TOOL init
```

This guides you through:

1. **Signing key** — if no key exists, offers to generate one at `~/.config/drift/keys/default.seed`.
2. **Publisher details** — author name and/or organization, email, website (informational, not cryptographic). At least one of author name or organization is required.
3. **Namespaces** — which Drift module namespaces this key will sign for (e.g. `acme.*`, `net_tls.*`). These must match the module names consumers import, not hyphenated package ids.
4. **Author profile** — writes a `.author-profile` file you share with consumers.

The private signing key stays local. The `.author-profile` is derived from the
signing key but contains only the public key and metadata — safe to share publicly.
At deploy time, Drift publishes a bound copy of this profile inside the versioned
artifact directory and signs an envelope that covers both the package digest and
the deployed profile digest.

For CI/automation, supply all fields via flags:

```bash
$DRIFT_TOOL init \
  --key ~/.config/drift/keys/default.seed \
  --name "Your Name" \
  --org "Your Org" \
  --namespace "acme.*" \
  --out acme.author-profile \
  --yes
```

Set the default signing key for other tooling:

```bash
export DRIFT_SIGN_KEY_FILE="$HOME/.config/drift/keys/default.seed"
```

## 2. Hello app (no dependency package)

Create a simple app:

```bash
mkdir -p sandbox/hello
cat > sandbox/hello/main.drift <<'DRIFT'
module main;

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
module mathlib;

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
module main;

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

This is the distribution-style flow for publishing signed packages.

### 5.1 Set up publishing identity

If you haven't already, run `drift init` (see section 1.7):

```bash
$DRIFT_TOOL init
```

This creates your signing key (if needed) and your `.author-profile`.

### 5.2 Sign the package

```bash
$DRIFT_TOOL sign sandbox/libmath/acme.math.dmp --key sandbox/keys/acme.seed --include-pubkey
```

This produces:
- `sandbox/libmath/acme.math.sig` (detached signature sidecar)

Standalone `drift sign` signs the package bytes only. The stronger
package+author-profile binding is added by `drift deploy`, which stages the
deployed `.author-profile` and signs an authenticated envelope over both
digests.

### 5.3 Trust the publisher (consumer side)

The consumer obtains the publisher's deployed `.author-profile` and trusts it:

```bash
$DRIFT_TOOL trust ~/opt/drift/libs/acme.math/0.1.0/.author-profile --trust-store sandbox/drift/trust.json
```

This displays the author's identity, key fingerprint, and namespace claims,
then asks for confirmation. When the profile comes from a deployed versioned
artifact directory, Drift verifies that the profile bytes are cryptographically
bound to the package signature. The consumer still verifies the key fingerprint
through an independent channel (website, email, etc.) — metadata is
informational even when integrity-bound.

Use `--yes` for non-interactive automation.

Inspect the trust store:

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

## 6. Prepare and deploy

The release workflow separates state preparation from publishing:

### 6.1 Prepare (resolve dependencies, write lock)

```bash
drift prepare --manifest drift-package.json --dest ~/opt/drift/libs
```

This resolves all package dependencies and writes `drift-lock.json`.
Review the lock, then commit it alongside your manifest.

### 6.2 Declare author profile in manifest

Every publishable project must declare its author profile in `drift-package.json`:

```json
{
  "schema_version": 1,
  "project": {
    "name": "acme-libs",
    "license": "MIT",
    "author_profile": "acme.author-profile"
  },
  "artifacts": [...]
}
```

Deploy will fail if `project.author_profile` is missing or the file does not exist.

### 6.3 Deploy (build, sign, smoke, publish)

```bash
drift deploy --manifest drift-package.json --dest ~/opt/drift/libs --driftc driftc
```

Deploy consumes the committed lock state. It builds, signs, smoke-tests,
and publishes all artifacts plus a bound copy of the declared `.author-profile`.

Deploy is read-only with respect to tracked project files — it does not
rewrite `drift-lock.json` or other repo-managed metadata.

Published layout for a package now looks like:

```text
~/opt/drift/libs/net-tls/0.3.4/
├── .author-profile
├── assets/
├── net-tls.sig
└── net-tls.zdmp
```

The deployed `.author-profile` is a published copy. `drift deploy` does not
rewrite the tracked project profile file after commit.

### 6.4 Intended workflow

1. `drift init` — create signing key + author profile (once per project)
2. Set `project.author_profile` in `drift-package.json`
3. Edit manifest (versions, deps, etc.)
4. `drift prepare` — resolve deps, write lock
5. Review changes, commit manifest + lock + author profile
6. `drift deploy` — build and publish from committed state

## 7. Revoke key (negative check)

Revoke by `kid`:

```bash
$DRIFT_TOOL trust revoke --trust-store sandbox/drift/trust.json --kid '<KID>' --reason 'test revoke'
```

Rebuild should now fail for that package.

## 8. Command checklist

**Publisher setup:**
- Initialize publishing identity: `drift init`
- Sign package: `drift sign <pkg.dmp> --key <seed>`

**Release workflow:**
- Prepare lock: `drift prepare --manifest drift-package.json --dest <dest>`
- Deploy: `drift deploy --manifest drift-package.json --dest <dest> --driftc <driftc>`

**Consumer trust:**
- Trust an author: `drift trust <file>.author-profile`
- List trust store: `drift trust list --trust-store <path>`
- Revoke a key: `drift trust revoke --trust-store <path> --kid <kid>`

**Build:**
- Build app: `driftc ... -o <exe>`
- Emit package: `driftc ... --emit-package <pkg.dmp> --package-id ... --package-version ... --package-target ...`

**Notes:**
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

## 9. Common pitfalls

- `module main` is required for default executable entrypoint (`main::main`).
- Imported module ids must match what the package exports.
- Author-profile namespace claims follow imported Drift module namespaces, not package ids (for example `net_tls.*`, not `net-tls.*`).
- If consuming unsigned local packages, pass `--skip-package-signatures` (and optionally `--allow-unsigned-from <dir>` when using raw `lang.driftc`).
- For signed flow, trust store must be configured (`--trust-store` or `DRIFT_TRUST_STORE`).
- `dist-publish-stdlib` requires a signing key (`DRIFT_SIGN_KEY_FILE` or explicit `SIGN_KEY` arg).
- For portability-sensitive environments, if toolchain asks for pointer width, add `--target-word-bits 64`.

## 10. Next expansion ideas

- Multi-package dependency chain (`pkg A -> pkg B -> app`).
- Multiple signatures on one package (multisig acceptance policy).
- CI recipe for publish/sign/trust/consume verification.
