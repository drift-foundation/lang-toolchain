# Drift Build & Package Workflow

> **Trust-model v1 cutover.**  The package trust model is defined in
> [`docs/design/trust-v1.md`](design/trust-v1.md).  Author signing
> lives in `drift author`; the consumer-side verifier
> reads `<pkg>.author-claim` + `<pkg>.cert-claim.<kid>.json`
> against a role-tagged `drift/trust.json`.  Pre-v1 `drift sign`,
> `.sig` sidecars, and the v0 envelope are gone.
>
> **Package-pool directory convention.**  The pool directory is
> `lib/` (matching the standard `bin/lib` split), NOT `libs/`.
> Earlier examples and certified pools may still use the plural
> form; that path keeps working because `drift prepare --package-root`
> is path-agnostic, but all new docs / scripts / orchestration
> should use `lib/`.  Pool operators wanting to roll over an
> existing `libs/` layout can publish to both for a transition
> window or symlink `lib → libs`.

This guide is a practical, end-to-end workflow for new developers:

1. Bootstrap and validate toolchain/compiler infra on a fresh clone.
2. Set up publishing identity.
3. Create a project manifest and build artifacts with `drift build`.
4. Prepare and deploy packages with `drift prepare` / `drift deploy`.
5. Trust and consume published packages.

Commands assume you run from repository root and use the local wrappers:

```bash
export DRIFTC="$PWD/bin/driftc"
export DRIFT_TOOL="$PWD/bin/drift"
```

Or the venv directly:

```bash
PYTHONPATH=. ./.venv/bin/python3 -m lang.driftc --help
PYTHONPATH=. ./.venv/bin/python3 -m lang.drift --help
```

## Trust layers (important)

For most users, use this simple model:

1. Publisher runs `drift init` to set up a signing key and public author profile.
2. `drift deploy` publishes the package and a deployed `<artifact>.author-profile` inside the versioned artifact directory.
3. The deployed signature binds both the package bytes and the deployed author profile.
4. Consumer obtains that deployed `<artifact>.author-profile` and runs `drift trust <file>.author-profile` to trust it.
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
- `just` runs the default full staged test path after `deps-check`.
- This is the recommended gate before coding in Drift.
- If you want to focus only on codegen/e2e for quick iteration: `just lang-codegen-test`.

### 1.6 Build runtime archives (required for archive-mode driftc)

```bash
just runtime-libs
```

Notes:
- `bin/driftc` always links the runtime from a pre-built variant archive.
- If archives are missing, compile fails with a runtime archive build error.

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

The private signing key stays local. The `.author-profile` file is derived from the
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

## 2. Create a project manifest

Every Drift project is defined by a `drift/manifest.json`. This is the single
source of truth for artifact structure, source inputs, dependencies, and build
configuration. Both `drift build` and `drift deploy` read it.

### 2.1 Manifest structure

```json
{
  "schema_version": 1,
  "project": {
    "name": "acme-libs",
    "license": "MIT",
    "author_profile": "acme.author-profile"
  },
  "artifacts": [
    {
      "kind": "package",
      "name": "acme-math",
      "version": "0.1.0",
      "description": "Math utilities",
      "entry_module": "src/mathlib.drift",
      "modules": ["src/mathlib.drift"]
    },
    {
      "kind": "app",
      "name": "acme-calc",
      "version": "0.1.0",
      "description": "Calculator app",
      "entry_module": "src/main.drift",
      "modules": ["src/main.drift"],
      "package_deps": [
        {"name": "acme-math", "version": "^0.1.0"}
      ]
    }
  ]
}
```

Key fields:

- **`kind`**: `"package"` (library `.dmp`) or `"app"` (executable binary).
- **`entry_module`**: the primary source file, always compiled first.
- **`modules`**: all source files for the artifact (entry module may appear here too — it is deduplicated).
- **`package_deps`**: dependencies on other Drift packages (semver constraints).
- **`unsafe`**: set to `true` for artifacts using C FFI (`extern "C"` calls).
- **`project.author_profile`**: path to `.author-profile` file (required for `drift deploy`, optional for `drift build`).

### 2.2 Machine-local configuration

Build inputs that vary per machine (library search paths, package roots) are
NOT stored in the manifest. They come from three sources, in precedence order:

1. Environment variables (`DRIFT_PACKAGE_ROOT`, `DRIFT_NATIVE_LIB_PATH`)
2. `drift/deploy-config.json` (colocated with manifest)
3. CLI flags (`--package-root`, `--native-lib-path`)

Roots from all three layers are concatenated into a single ordered list.
Within `DRIFT_PACKAGE_ROOT` the colon-separated order is preserved; within
`deploy-config.json` and `--package-root` the array / repeat order is
preserved. The resolver iterates the merged list and **first-root-wins on
`(package_id, version)` collisions** — different versions of the same
package both stay in the index and are selected by lockfile / `--dep`
pinning.

All paths must be absolute.

### 2.3 Selective overlay (staging → certified)

Short-term migration overlap — for example, an app team consuming
`certified/current/lib` while another team has shipped a patch to
`staging/` that the app team needs to integrate against before the next
certification cycle — is the supported overlay use case. Order
`DRIFT_PACKAGE_ROOT` (or the equivalent config / CLI layer) so the
staging root precedes the certified root, then regenerate the lock:

```bash
# 1. Set ordered roots (staging first, certified second).
export DRIFT_PACKAGE_ROOT=/abs/path/to/staging/lib:/abs/path/to/certified/current/lib

# 2. Regenerate the lock against the overlayed roots.  drift-web (or
#    whichever package staging publishes) pins to staging's exact
#    identity; every other dep continues to pin to its certified
#    identity via first-root-wins on non-shadowed (pkg_id, version)
#    pairs.
drift prepare --manifest drift/manifest.json

# 3. Build with the same DRIFT_PACKAGE_ROOT in scope.
drift build --manifest drift/manifest.json --driftc $DRIFTC
```

What this gets you:

- **Surgical override per package** via root ordering. Staging-only
  packages overlay; non-staging packages still resolve from certified.
- **Provenance preserved.** The regenerated lockfile (schema v4) records
  staging's `sha256` / `author_key` / `source_content_id` /
  `source_attestation_key` for the overlayed dep, certified's identity
  for everything else. The lockfile is the audit trail — no hand-copying
  with lost provenance.
- **Uncertified by definition.** An overlay build's output is not a
  certified snapshot. Promotion / certify-lane semantics are unchanged;
  only the consumer's local build floats. Steady state is: staging's
  patch gets re-certified, app team picks up the new
  `certified/current`, the overlay is removed.

If staging publishes a *different* version (e.g. `drift-web@0.4.1` vs
certified's `0.4.0`), `drift prepare` picks staging's version through
ordinary semver constraint resolution. If staging publishes the *same*
version with different content, first-root-wins on the index and the
regenerated lock pins staging's identity explicitly — the v4 identity
fields make silent same-version-different-bytes substitution
detectable on later verification, by design.

For a single-package surgical pin without regenerating the lock:

```bash
drift build --manifest drift/manifest.json --driftc $DRIFTC \
  --dep drift-web@0.4.1
```

`--dep PKG@VERSION` (added in 0.27.48) selects the exact consumed
version from whichever root provides it.

**This pattern is the bridge for migration overlap, not a long-term
consumption mode.** Overlay builds are uncertified by design; the
expected steady state is re-certification of the staged patch and a
fresh `certified/current` snapshot.

## 3. Build artifacts with `drift build`

`drift build` is the manifest-driven local build command. It reads
`drift/manifest.json`, resolves dependencies, and invokes `driftc` with the
correct flags.

### 3.1 Build a single artifact

If the manifest has exactly one artifact, the name is optional:

```bash
drift build --manifest drift/manifest.json --driftc $DRIFTC
```

For multi-artifact manifests, specify which one:

```bash
drift build acme-math --manifest drift/manifest.json --driftc $DRIFTC
drift build acme-calc --manifest drift/manifest.json --driftc $DRIFTC
```

### 3.2 Default output paths

| Artifact kind | Default output              |
|---------------|-----------------------------|
| `package`     | `build/<artifact-name>.dmp` |
| `app`         | `build/<artifact-name>`     |

Override with `-o`:

```bash
drift build acme-math -o /tmp/acme-math.dmp --driftc $DRIFTC
```

### 3.3 Dependency resolution

`drift build` does NOT own resolution. It consumes existing state:

- If `drift/lock.json` exists next to the manifest, it uses the locked
  compatibility graph (direct + transitive). The lock pins major.minor
  version ranges and author keys — any compatible patch within the range
  is accepted. A stale or partial lock is an error.
- If no lockfile exists, only exact version pins are accepted. Range
  constraints (e.g. `^1.0.0`) require a lockfile — run `drift prepare` first.

### 3.4 Passthrough flags

Flags after `--` are forwarded directly to `driftc`:

```bash
drift build acme-math -- --verbose --json
```

### 3.5 Package metadata contract

For package artifacts, `drift build` emits exact resolved versions in the
package metadata (`--package-dep`), not the manifest's author-intent ranges.
Only direct dependencies appear as declared package deps — transitive
dependencies are used for compiler version selection (`--dep`) but are not
embedded in the published metadata.

## 4. Prepare and deploy

The release workflow separates state preparation from publishing:

### 4.1 Prepare (resolve dependencies, write lock)

```bash
drift prepare --manifest drift/manifest.json --dest ~/opt/drift/lib
```

This resolves all package dependencies and writes `drift/lock.json`.
The lock records the compatibility contract: major.minor version range
and author signing key for each dependency. Patch updates within the
range are accepted silently; minor/major bumps and key rotation require
re-running prepare. Review the lock, then commit it alongside your manifest.

### 4.2 Declare author profile in manifest

Every publishable project must declare its author profile in `drift/manifest.json`
(see section 2.1). Deploy will fail if `project.author_profile` is missing or the
file does not exist.

### 4.2a Author-publish + project trust bootstrap (one-time)

`drift deploy` consumes — it does not produce — the project's
author claims and trust store.  Each library artifact needs two
committed files in `drift/`:

```text
drift/<pkg>.author-claim          # signed body (drift author)
drift/<pkg>.author-pubkey.b64     # base64 pubkey of the signer
```

Both are emitted together by:

```bash
drift author --manifest drift/manifest.json --key-file <author.seed>
```

(Repeat with `--overwrite` after any manifest change that affects
source identity, e.g. version bumps, module renames, asset
additions.  See `drift trust check` below — it surfaces stale
claims with the `version_mismatch` / `sci_mismatch` codes.)

**Migrating an existing repo** whose author claims were minted
before the pubkey-companion was emitted: re-run
`drift author --manifest drift/manifest.json --overwrite`
for each artifact, or hand-write the companion:

```bash
echo "<base64-32-byte-pubkey>" > drift/<pkg>.author-pubkey.b64
```

The kid derived from the companion must match a signer of the
existing claim; bootstrap will refuse a mismatched pair.

Once both files exist, set up the project trust store:

```bash
drift trust bootstrap --manifest drift/manifest.json
```

This derives `drift/trust.json` from the on-disk sidecars and
grants the **author** role for the claim's namespaces.

Then add the **certifier** role separately — `bootstrap`
deliberately does not grant certifier, because the kid that signs
cert claims (`DRIFT_SIGN_KEY_FILE` or whatever the deploy uses)
is a deploy-time concern, not an author-time one:

```bash
# Same-key team (operator's seed plays both roles)
drift trust add --trust-store drift/trust.json \
    --namespace '<art-namespace>.*'            \
    --pubkey-b64 "$(cat drift/<pkg>.author-pubkey.b64)" \
    --role certifier

# Split-key team (orch certifier is a different identity)
drift trust add --trust-store drift/trust.json \
    --namespace '<art-namespace>.*'            \
    --pubkey-b64 <orch-certifier-pubkey-b64>   \
    --role certifier
```

Preflight (read-only) before any deploy attempt:

```bash
# Bare form — catches missing/stale author claims + trust store.
# Does NOT require the pubkey companion (only bootstrap does).
drift trust check --manifest drift/manifest.json

# Recommended form for orch and CI:
# also verifies the expected certifier kid is granted `certifiers`,
# so a missing cert-role grant fails preflight instead of at smoke.
drift trust check --manifest drift/manifest.json \
    --certifier-key-file "$DRIFT_SIGN_KEY_FILE"
```

Exit code is `0` when ready, `1` when not — gate the deploy on
the return code.  Errors carry stable `code` strings
(`trust_store_missing`, `version_mismatch`, `sci_mismatch`,
`author_not_trusted`, `certifier_not_trusted`,
`legacy_sig_present`, ...) so CI matchers can act on specific
failure modes.

### 4.3 Deploy (build, sign, smoke, publish)

```bash
drift deploy --manifest drift/manifest.json --dest ~/opt/drift/lib --driftc driftc
```

Deploy consumes the committed lock state. It builds, signs, smoke-tests,
and publishes all artifacts plus a bound copy of the declared author profile.

Deploy is read-only with respect to tracked project files — it does not
rewrite `drift/lock.json` or other repo-managed metadata.

Published layout for a package (trust-v1):

```text
~/opt/drift/lib/net-tls/0.3.4/
├── assets/
├── net-tls.author-profile
├── net-tls.author-claim                 # author claim (drift author)
├── net-tls.cert-claim.<kid>.json        # cert claim (emitted by `drift deploy`)
└── net-tls.zdmp
```

The deployed author profile is a published copy. `drift deploy` does not
rewrite the tracked project profile file after commit.

### 4.4 Intended workflow

1. `drift init` — create signing key + author profile (once per project)
2. Create `drift/manifest.json` with `project.author_profile` set
3. `drift build <artifact>` — iterate locally
4. `drift prepare` — resolve deps, write lock
5. Review changes, commit manifest + lock + author profile
6. `drift deploy` — build and publish from committed state

## 5. Trust and consume published packages

### 5.1 Trust an author (consumer side)

The consumer obtains the publisher's deployed author profile and trusts it:

```bash
$DRIFT_TOOL trust ~/opt/drift/lib/acme-math/0.1.0/acme-math.author-profile --trust-store drift/trust.json
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
$DRIFT_TOOL trust list --trust-store drift/trust.json --json
```

### 5.2 Revoke key (negative check)

Revoke by `kid`:

```bash
$DRIFT_TOOL trust revoke --trust-store drift/trust.json --kid '<KID>' --reason 'compromised'
```

Rebuild should now fail for packages signed by that key.

### 5.3 Signed stdlib into local dist repo (creator flow)

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

## 6. Direct driftc usage (low-level / ad hoc)

For single-file experiments, manual compiler exploration, or workflows that
don't need a manifest, you can invoke `driftc` directly:

### 6.1 Build a single-file app

```bash
$DRIFTC --stdlib-root stdlib sandbox/hello/main.drift -o sandbox/hello/hello_app
```

### 6.2 Emit a package artifact

```bash
$DRIFTC -M sandbox/libmath sandbox/libmath/mathlib.drift \
  --package-id acme.math --package-version 0.1.0 \
  --package-target drift-dev --emit-package sandbox/libmath/acme.math.dmp
```

### 6.3 Consume a package (unsigned local flow)

```bash
$DRIFTC -M sandbox/app \
  --package-root sandbox/libmath \
  --allow-unsigned-from sandbox/libmath \
  --stdlib-root stdlib \
  sandbox/app/main.drift -o sandbox/app/my_app
```

### 6.4 Sign a package manually (trust-v1)

Author claim emission (signs source identity, *not* artifact bytes).
The CLI is manifest-aware: it reads `drift/manifest.json` and
computes SCI itself, so the digest in the author claim is
byte-identical to the one `drift build` / `drift deploy` will
stamp into the `.dmp` (trust-v1.md §3.5 three-way equality):

```bash
drift author                                          \
    --manifest sandbox/libmath/drift/manifest.json    \
    --key-file ~/.config/drift/keys/default.seed
```

This produces `sandbox/libmath/drift/acme.math.author-claim` (next
to the manifest, which is also where `drift deploy` looks for it).
Optional flags: `--artifact <name>` when the manifest declares
multiple library artifacts; `--namespace <glob>` (repeatable) to
override the default `<art.module_namespace>.*`; `--release-utc
<iso>` to pin the timestamp (default: now); `--sidecar-dir <dir>`
to override the output location; `--overwrite` to replace.

Cert claim emission (signs artifact bytes + dep graph + cert
suite) happens through `drift deploy` (the certifier role);
manual single-package cert emission is intentionally not exposed
because the cert claim is meaningful only as the output of a
certifier pipeline that observed the build and ran a suite.

The internal `python -m tools.drift_author publish-raw` entry
point still exists for the toolchain's own stdlib release (which
computes SCI outside the v2 manifest machinery) and for
co-signing (`python -m tools.drift_author cosign`).  Package
authors should always reach for `drift author` instead.

See [`docs/design/trust-v1.md`](design/trust-v1.md) §7 for the
full author / certifier workflow.

## 7. Command checklist

**Publisher setup:**
- Initialize publishing identity: `drift init`
- Mint author claim: `drift author --manifest drift/manifest.json --key-file <seed>` (reads the manifest, computes SCI via the shared helper, derives body fields, signs)

**Project build (manifest-driven):**
- Build artifact: `drift build <artifact> --manifest drift/manifest.json --driftc <driftc>`

**Release workflow:**
- Prepare lock: `drift prepare --manifest drift/manifest.json --dest <dest>`
- Deploy + emit cert claim: `drift deploy --manifest drift/manifest.json --dest <dest> --driftc <driftc>`

**Consumer trust:**
- Trust an author profile: `drift trust <file>.author-profile`
- Grant role manually: `drift trust add --namespace <glob> --pubkey-b64 <b64> --kid <kid> --role author|certifier|both`
- Bulk-import from a v1 author claim: `drift trust import <pkg>.author-claim [--role author|both]`
- List trust store: `drift trust list --trust-store <path>`
- Revoke a key: `drift trust revoke --trust-store <path> --kid <kid>`

**Direct compiler (ad hoc):**
- Build app: `driftc ... -o <exe>`
- Emit package: `driftc ... --emit-package <pkg.dmp> --package-id ... --package-version ... --package-target ...`

**Notes:**
- Signature verification is default in `bin/driftc` package mode.
- Opt-out only when needed: `--skip-package-signatures`
- `DRIFT_ASAN=1` is supported in direct `bin/driftc` compile/link mode and injects `-fsanitize=address -g`.
- `DRIFT_MEMCHECK=1` and `DRIFT_MASSIF=1` are runner-only (execution-time) toggles; `bin/driftc` fails fast if they are set.
- Runtime linking is always static-archive mode (`libdrift_rt[_debug]_abi<N>.a` variants under `build/runtime_libs/`).
- If the archive build fails, `driftc` fails with a clear error.
- Set `DRIFT_RUNTIME_LIB_CACHE_DIR=<path>` to redirect runtime archive artifacts/locks to a caller-writable location.
- Legacy-compatible override: `DRIFT_RUNTIME_BUILD_ROOT=<path>` writes archives under `<path>/runtime_libs/`.

## 8. Common pitfalls

- `module main` is required for default executable entrypoint (`main::main`).
- Imported module ids must match what the package exports.
- Author-profile namespace claims follow imported Drift module namespaces, not package ids (for example `net_tls.*`, not `net-tls.*`).
- If consuming unsigned local packages with direct driftc, pass `--skip-package-signatures` (and optionally `--allow-unsigned-from <dir>`).
- For signed flow, trust store must be configured (`--trust-store` or `DRIFT_TRUST_STORE`).
- `dist-publish-stdlib` requires a signing key (`DRIFT_SIGN_KEY_FILE` or explicit `SIGN_KEY` arg).
- For portability-sensitive environments, if toolchain asks for pointer width, add `--target-word-bits 64`.
