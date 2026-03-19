# drift deploy: Standardized Package Deploy Tool

## Problem

Every downstream Drift package team currently invents its own deploy
workflow.  Each team writes ad hoc build/deploy tooling, README-driven
link flag instructions, and custom smoke tests.  This means:

- Native dependency requirements (`--link-lib ssl`) are communicated via
  README, not machine-readable metadata.
- Signing, staging, and publish are reimplemented per-package.
- Smoke test contracts vary — some compile a consumer, some hit a server,
  some do nothing.
- Policy changes (signing rules, metadata requirements, new link flags) must
  be propagated to every downstream deploy implementation individually.
- Repos that produce multiple artifacts (a library and a CLI tool, or
  multiple libraries with different dependency graphs) must either split
  into separate repos or maintain ad hoc multi-artifact build logic.

The existing compiler/stdlib deploy pipeline (5 steps: PEX build, bundle,
stdlib sign, smoke, publish) demonstrates the right pattern but is
hardcoded for the compiler distribution.  Downstream packages need the
same rigor without copying the orchestrator.

## Goal

One standard `drift deploy` tool that downstream projects use by providing:

1. A project manifest (`drift-manifest.json`) — the authoritative source
   of project identity and per-artifact build/deploy configuration.
2. Optionally, per-artifact smoke commands for package-specific integration
   validation.

The tool enforces standard policy for artifact build, signing, native
dependency declaration, metadata emission, staged smoke validation, and
atomic publish.  Policy evolution happens once (in the tool), not per-repo.

The manifest supports multiple artifacts from a single repo.  Each
artifact has its own kind, identity, version, dependencies, and smoke
command.

---

## 1. Manifest Format

### Location

`drift-manifest.json` at the project source root.

### Design principles

- JSON, not TOML/YAML.
- Author-centric: contains only things the project author knows and owns.
- Multi-artifact: one manifest describes all artifacts the repo produces.
- No machine-local details (signing env vars, library search paths, tool
  internals).
- No redundancy with information the tool can derive (target triple from
  host, compiler version from `driftc --version`).

### MVP schema

`drift-manifest.json` is the single authoritative source of truth for
project identity, per-artifact configuration, build inputs, dependencies,
and smoke configuration.  The tool reads everything from this file.

This is a deliberate standardization decision: `drift-manifest.json`
owns both project/artifact identity and build inputs.  Including
`entry_module` and `modules` on each artifact (rather than leaving
them as CLI-only arguments) means the manifest fully describes what
gets built, not just what gets deployed.  The rationale:

- An artifact's source inputs are as stable as its identity — they
  change at the same cadence and for the same reasons.
- Separating "what to build" from "what to deploy" creates a second
  coordination surface (Makefile/justfile must agree with manifest).
- The tool should be invocable with `drift deploy --dest <dest>` and
  nothing else — all build inputs come from the manifest.
- This is the same model as `package.json` (npm), `Cargo.toml`
  (Rust), and `pyproject.toml` (Python): identity + build inputs +
  metadata in one file.

```json
{
  "schema_version": 1,
  "project": {
    "name": "acme",
    "license": "Apache-2.0"
  },
  "artifacts": [
    {
      "kind": "package",
      "name": "net.tls",
      "version": "0.3.0",
      "description": "TLS library for Drift",
      "entry_module": "src/net_tls/lib.drift",
      "modules": ["src/net_tls/"],
      "native_deps": [
        {"lib": "ssl"},
        {"lib": "crypto"}
      ],
      "assets": ["docs/net_tls/"],
      "smoke_command": ["just", "smoke-net-tls"]
    },
    {
      "kind": "app",
      "name": "tls-tool",
      "version": "0.3.0",
      "description": "TLS CLI tool",
      "entry_module": "src/tls_tool/main.drift",
      "modules": ["src/tls_tool/"],
      "package_deps": [
        {"name": "net.tls", "version": "^0.3.0"}
      ],
      "smoke_command": ["just", "smoke-tls-tool"]
    }
  ]
}
```

### Project-level vs artifact-level fields

The manifest has two levels.  Project-level fields describe the repo
as a whole.  Artifact-level fields describe each individual output.

**Rule**: a field is project-level if it applies uniformly to all
artifacts in the repo.  A field is artifact-level if it can
meaningfully differ between artifacts in the same repo.

| Field            | Level      | Rationale                                          |
|------------------|------------|----------------------------------------------------|
| `schema_version` | top-level  | Manifest format version, not per-artifact           |
| `project.name`   | project    | Repo/org identity, shared across artifacts          |
| `project.license`| project    | Legal default for the project                       |
| `kind`           | artifact   | Each artifact has its own output type               |
| `name`           | artifact   | Each artifact has its own package/app identity      |
| `version`        | artifact   | Artifacts may version independently                 |
| `description`    | artifact   | Each artifact has its own purpose                   |
| `license`        | artifact   | Override project license for a specific artifact    |
| `entry_module`   | artifact   | Each artifact has its own entry point               |
| `modules`        | artifact   | Each artifact has its own source set                |
| `package_deps`   | artifact   | Each artifact has its own Drift dependency graph    |
| `native_deps`    | artifact   | Each artifact has its own native library needs      |
| `assets`         | artifact   | Each artifact ships its own documentation/examples  |
| `smoke_command`  | artifact   | Each artifact has its own validation needs          |

**Inheritance rule for `license`**: if an artifact does not specify
`license`, it inherits from `project.license`.  If an artifact
specifies `license`, it overrides the project default.
`project.license` is required.  This is the only field with
inheritance — all other artifact fields are independent.

**`version` is always artifact-level**: there is no project-level
version.  Artifacts in the same repo may version independently
(e.g., `net.tls` 0.3.0 and `tls-tool` 0.3.0 may diverge later).
If a project wants all artifacts at the same version, they repeat
the value.  The manifest does not enforce version lockstep.

### Field reference: top-level

| Field            | Type            | Required | Description                                      |
|------------------|-----------------|----------|--------------------------------------------------|
| `schema_version` | integer         | yes      | Manifest schema version (must be `1` for MVP)    |
| `project`        | Project         | yes      | Project-level metadata                           |
| `artifacts`      | Artifact[]      | yes      | Array of artifact definitions (min 1)            |

### Field reference: Project object

| Field            | Type            | Required | Description                                      |
|------------------|-----------------|----------|--------------------------------------------------|
| `name`           | string          | yes      | Project identity (e.g. org or repo name)         |
| `license`        | string          | yes      | SPDX license identifier — default for all artifacts |

### Field reference: Artifact object

| Field            | Type            | Required | Description                                      |
|------------------|-----------------|----------|--------------------------------------------------|
| `kind`           | string          | yes      | Artifact type: `"package"` or `"app"`            |
| `name`           | string          | yes      | Artifact identity (dot-separated for packages)   |
| `version`        | string          | yes      | SemVer artifact version                          |
| `description`    | string          | yes      | Short human-readable artifact description        |
| `license`        | string          | no       | SPDX override (default: `project.license`)       |
| `entry_module`   | string          | yes      | Path to primary source file (relative to manifest) |
| `modules`        | string[]        | yes      | Source directories / files to include             |
| `package_deps`   | PackageDep[]    | no       | Drift package dependencies (default: none)       |
| `native_deps`    | NativeDep[]     | no       | Native library requirements (default: none)      |
| `assets`         | string[]        | no       | Files/directories to include in published install (default: none) |
| `smoke_command`  | string[]        | no       | Smoke test command as argv array (default: built-in baseline smoke) |

`smoke_command` is an argv array, not a shell string.  The tool invokes
it directly via `execvp` / `subprocess.run` (no shell interpretation).
The command can be any executable: a `just` recipe, a Python module, a
compiled binary, or anything else on PATH.

### Artifact kinds

The MVP supports two artifact kinds:

| Kind      | What it produces     | Signed | Published to package root | Consumable by `driftc --package-root` |
|-----------|----------------------|--------|---------------------------|---------------------------------------|
| `package` | `.dmp` + `.dmp.sig`  | yes    | yes                       | yes                                   |
| `app`     | Compiled binary      | no     | no (separate `--app-dest`)| no                                    |

**`package`**: a shared library artifact.  Built via
`driftc --emit-package`, signed, published to the versioned package
directory layout.  This is what consumers import.

**`app`**: an executable artifact.  Built via `driftc` (standard
compilation to binary), published to a separate application
destination.  Apps are not published to the package root.

**App trust boundary**: app artifacts are **unsigned** in MVP.  The
tool does not sign app binaries or produce `.sig` sidecars for them.
Apps are not consumed as packages by other builds — they are
end-user executables.  The trust model for app distribution (binary
signing, notarization, code signing certificates) is outside the
scope of `drift deploy`.  If app signing is needed, it is the
responsibility of the deployment environment, not the package tool.
This is a deliberate MVP scope boundary: package signing
authenticates the package contract consumed by `driftc`; app
distribution authentication is a different problem with different
mechanisms.

Apps may depend on packages (via `package_deps`), but packages must
not depend on apps.  The tool validates this: a `package` artifact
with a `package_deps` entry referencing an `app` artifact is an
error.

### PackageDep object (MVP)

| Field      | Type              | Required | Description                                   |
|------------|-------------------|----------|-----------------------------------------------|
| `name`     | string            | yes      | Drift package identity (dot-separated)        |
| `version`  | string            | yes      | SemVer version constraint                     |

Version constraints follow npm-style semver range syntax:

| Syntax              | Meaning                                                    |
|---------------------|------------------------------------------------------------|
| `1.2.3`             | Exact version match                                        |
| `^1.2.3`            | Compatible with 1.2.3 (≥1.2.3, <2.0.0)                    |
| `~1.2.3`            | Approximately 1.2.3 (≥1.2.3, <1.3.0)                      |
| `>=1.2.3 <2.0.0`    | Explicit range                                             |

Caret (`^`) is expected to be the common default.

`package_deps` declares logical Drift package dependencies.
Resolution uses directory-based package roots — the tool scans
`--package-root` directories for matching `<package>/<version>/`
entries.  No central registry is assumed or required.

`package_deps` and `native_deps` are separate dependency classes
with separate resolution domains:

| Section        | Describes                          | Resolution domain          |
|----------------|------------------------------------|----------------------------|
| `package_deps` | Drift package dependencies         | Drift package roots        |
| `native_deps`  | Native system/linker dependencies  | System library paths       |

A Drift package dependency is resolved by the compiler/tool from
package roots.  A native dependency is resolved by the system linker
from library paths.  These are fundamentally different resolution
mechanisms.

### NativeDep object (MVP)

| Field      | Type              | Required | Description                                   |
|------------|-------------------|----------|-----------------------------------------------|
| `lib`      | string            | yes      | Linker library token (passed as `-l<lib>`)    |

`lib` is deliberately named to reflect its exact semantics: a linker
token appended as `-l<lib>` to the link command.  It is not a logical
dependency name, provider identifier, or package-level concept.

All declared native deps are required.  There is no `required` field
in the MVP schema — every declared library is unconditionally passed
to the linker.  Optional/conditional native deps are deferred until
real availability-probe semantics exist (see future extensions).

**Future direction**: the MVP `lib` form covers direct linker tokens
only.  A later schema version may introduce a richer native dependency
model with logical provider references that resolve through a metadata
layer — for example `{"provider": "openssl"}` or `{"spec": "openssl"}`
representing a logical dependency with platform-specific library names,
discovery rules, static/shared preferences, and version constraints.
The `lib` field is reserved for direct linker tokens; `provider`/`spec`
concepts are reserved for the future indirection model.

### Version source rule

`drift-manifest.json` is the standard version source for all artifacts
using `drift deploy`.

- The tool reads `name` and `version` from each artifact and passes
  them to `driftc` (e.g., `--package-id <name> --package-version
  <version>` for package artifacts).
- The tool does not reconcile version from multiple sources.
- Repos must not split version across `VERSION` files, Makefile
  variables, Python constants, or other ad hoc locations once they
  adopt this standard.
- If a repo currently maintains version elsewhere, it must either
  migrate to `drift-manifest.json` or generate `drift-manifest.json`
  from its existing source before invoking the tool.

One file, per-artifact values, no drift.

### Schema versioning and compatibility

`schema_version` is mandatory.  The MVP version is `1`.

- The tool checks `schema_version` before reading any other field.
- If `schema_version` is missing, the tool fails with a clear error:
  `error: drift-manifest.json missing required field 'schema_version'`.
- If `schema_version` is higher than the tool understands, the tool
  fails with a clear error:
  `error: drift-manifest.json schema_version 3 is not supported by this
  version of drift deploy (supports up to version 1). Upgrade
  drift deploy.`
- The tool does not attempt to guess, partially parse, or fall back
  when it encounters an unknown schema version.  Fail clearly, not
  silently.

Compatibility policy:

- New schema versions may add required fields, change field semantics,
  or restructure sections.  There is no promise of forward
  compatibility — newer schemas require a newer tool.
- Older manifests continue to work with newer tools: the tool supports
  all schema versions up to its maximum.  This is backward
  compatibility only, not forward.
- Schema version bumps are deliberate and documented.  They happen
  when a field change would alter the meaning of an existing manifest.

### Artifact name uniqueness

Artifact names must be unique within the manifest.  Two artifacts
with the same `name` is a validation error:
`error: duplicate artifact name 'net.tls' in drift-manifest.json`

### What the manifest does NOT contain

- `DRIFT_SIGN_KEY_FILE` or similar env var names — signing key location is
  runtime configuration, not project metadata.
- `--package-target` — derived from the build host or `--target` CLI flag.
- Library search paths — consumer-side configuration, not project metadata.
- Detached-signature toggle — detached is the only mode.
- Tool version pins — the tool manages its own compatibility.
- Output paths — the tool owns staging layout.

### Future extensions (non-MVP)

```json
{
  "native_deps": [
    {
      "lib": "ssl",
      "required": false,
      "pkg_config": "openssl",
      "platform": {"linux": "ssl", "darwin": "ssl", "windows": "libssl"},
      "min_version": "1.1.0"
    },
    {
      "provider": "openssl",
      "min_version": "1.1.0"
    }
  ],
  "pre_build_command": ["python3", "generate_bindings.py"],
  "consumers": ["tests/consumer_basic.drift"]
}
```

---

## 2. Native Dependency Metadata

### Where it lives

Native dependency metadata has two homes, serving two audiences:

1. **`drift-manifest.json`** — the author's source-of-truth declaration,
   per artifact.  Human-readable, version-controlled, used by
   `drift deploy` during the build.

2. **Inside the `.dmp` manifest** — the machine-readable contract for
   consumers.  `drift deploy` copies the artifact's `native_deps` into
   the `.dmp` package manifest at build time.

The `.dmp` manifest is inside the signed payload.  This means native
dependency metadata is authenticated: modifying it invalidates the
Ed25519 signature.  No separate signed sidecar needed for native deps.

Native deps apply to `package` artifacts only.  `app` artifacts may
declare `native_deps` for link-time use, but since apps are not
consumed as packages, their native deps are not embedded in a `.dmp`.

### `.dmp` manifest schema (new field)

```json
{
  "format": "dmir-pkg",
  "format_version": 0,
  "package_id": "net.tls",
  "package_version": "0.3.0",
  "target": "x86_64-linux-gnu",
  "native_deps": {
    "schema_version": 1,
    "link_libs": [
      {"lib": "ssl"},
      {"lib": "crypto"}
    ]
  },
  "package_deps": [
    {"name": "acme.crypto", "version": "^0.2.0"}
  ],
  "modules": [...],
  "blobs": {...}
}
```

Omitting `native_deps` means no native dependencies.
Omitting `package_deps` means no Drift package dependencies.

Both `native_deps` and `package_deps` are inside the signed payload.

### MVP limitations

The MVP is a **linker-token model**, not a native dependency resolver.
Each `native_deps` entry is a direct `-l<lib>` token passed to the
linker.  There is no indirection, discovery, or resolution layer.

The MVP reliably supports:

- Bare `-l<lib>` flags appended to the linker command.
- Deduplication when multiple packages declare the same `lib` token.
- Enriched diagnostics when a declared library is missing at link time.

The MVP does **not** provide:

- Automatic library discovery (no `pkg-config`, no `cmake find_package`).
- Logical dependency resolution (no provider/spec indirection).
- Library search path resolution — consumers on non-standard library
  paths must still pass `--link-search <dir>` manually.
- Version constraint checking — the consumer is responsible for having
  a compatible version installed.
- Platform-conditional library names — packages targeting multiple
  platforms must build separate `.dmp` artifacts per target (which is
  already the case since `target` is per-package).
- Static vs dynamic linking control.

In practice, phase 1 means: if the consumer has the required native
libraries installed in standard system paths, auto-linking works.  If
libraries are in non-standard locations, or the consumer needs specific
versions, manual `--link-search` or `--link-lib` flags may still be
needed.  Integration guides should document prerequisites but are no
longer the canonical dependency declaration mechanism — the `.dmp`
manifest is.

### How the compiler consumes it

When `driftc` loads a `.dmp` via `load_package_v0_with_policy`, it reads
`native_deps` and `package_deps` and stores them on `LoadedPackage`:

```python
@dataclass(frozen=True)
class NativeDepEntry:
    lib: str

@dataclass(frozen=True)
class PackageDepEntry:
    name: str
    version: str  # semver constraint

@dataclass(frozen=True)
class LoadedPackage:
    # ... existing fields ...
    native_deps: list[NativeDepEntry]    # NEW; empty if absent
    package_deps: list[PackageDepEntry]  # NEW; empty if absent
```

At link time, after all packages are loaded:

```python
merged_native_libs: list[str] = []
seen: set[str] = set()
for pkg in loaded_pkgs:
    for dep in pkg.native_deps:
        if dep.lib not in seen:
            seen.add(dep.lib)
            merged_native_libs.append(dep.lib)

# Append to link command after user-specified --link-lib flags
for lib in merged_native_libs:
    link_cmd.extend([f"-l{lib}"])
```

Rules:
- All declared deps are required (no optional deps in MVP).
- Deduplicate by name across packages.
- Consumer `--link-lib` flags appear first (consumer can override).
- `--no-package-native-deps` suppresses auto-linking.

### Producer-side emission

New `driftc` flags for `--emit-package`:

```
--native-link-lib <LIB>    Declare native library dependency (repeatable)
--package-dep <NAME>=<VER> Declare Drift package dependency (repeatable)
```

`drift deploy` passes these flags automatically based on each artifact's
configuration in `drift-manifest.json`.

### Signing / trust separation

| Concern                   | Where                    | Who controls         |
|---------------------------|--------------------------|----------------------|
| What native libs needed   | `.dmp` manifest          | Package producer     |
| What packages needed      | `.dmp` manifest          | Package producer     |
| Who signed the package    | `.dmp.sig` sidecar       | Package producer     |
| Whether to trust signer   | `drift/trust.json`       | Package consumer     |
| Trust roots / public keys | Consumer trust store     | Package consumer     |

Native deps and package deps are package metadata.  Trust roots are
consumer policy.  The dependency schemas contain no trust material.

### Colocation

```
pkg.dmp          # signed package (native_deps, package_deps inside manifest)
pkg.dmp.sig      # detached signature
```

No additional files.  Signature covers the full `.dmp` byte stream,
which includes the manifest containing both dependency sections.

---

## 3. Package Assets

### Purpose

Downstream packages often ship files alongside the `.dmp` artifact:
documentation, examples, integration guides, license files, migration
notes, etc.  Without standard tooling, each team adds custom copy
logic to their deploy flow.  The `assets` manifest field makes the
standard tool responsible for staging and publishing these files.

Assets are per-artifact.  Each artifact in the manifest declares its
own `assets` list.

### Semantics

- Asset paths in the manifest are relative to `drift-manifest.json`.
- Paths may reference files or directories.
- Directories are copied recursively.
- The tool preserves relative path structure under the install root.

### Staged and published layout

Assets are placed in an `assets/` subdirectory of the install root,
preserving their relative paths:

```
<dest>/<package>/<version>/
    <package>.dmp
    <package>.dmp.sig
    assets/
        docs/
            integration.md
            api.md
        README.md
```

The `assets/` prefix keeps shipped files separate from the package
artifact (`.dmp` + `.dmp.sig`) at the install root.

### Interaction with staging and smoke

Assets are copied into the staged install directory before the smoke
command runs.  Smoke commands can access them via
`$DRIFT_STAGED_INSTALL/assets/`.

### Validation

- If a declared asset path does not exist at build time, the tool
  fails with a clear error:
  `error: artifact 'net.tls': declared asset not found: docs/missing.md`
- Empty directories are allowed (copied as-is).
- Symlinks within asset paths are followed (resolved to regular files).
- The tool does not validate asset content — it copies verbatim.

### Signing, trust, and the authenticated boundary

Assets are **not** inside the signed `.dmp` payload.  The
authenticated package contents are exactly two files:

- `<package>.dmp` — signed package artifact (compiled modules, type
  information, MIR, native dependency declarations, package dependency
  declarations)
- `<package>.dmp.sig` — Ed25519 detached signature over the `.dmp`

Everything else in the install directory — including `assets/` — is
**unauthenticated colocated content**.  The `.dmp.sig` covers only
the `.dmp` byte stream.

Operational rules:

- Build, link, and integration logic **must not** rely on `assets/`
  integrity for security-sensitive behavior.  Assets are not part of
  the signed package contract.
- Documentation, examples, integration guides, and license files are
  appropriate asset content.
- Integrity-sensitive package semantics (compiled code, type metadata,
  native dependency declarations, ABI contracts) belong inside the
  `.dmp`, not in `assets/`.
- If a future use case requires authenticated ancillary files (e.g.,
  bundled native object files), that would be a separate mechanism
  with its own integrity verification, not an extension of the
  `assets` field.

### Interaction with exact-version publish

Assets are published inside the versioned directory alongside the
`.dmp` and `.dmp.sig`.  Each version has its own complete set of
assets.  There is no sharing or deduplication across versions — each
published version is self-contained.

---

## 4. Staged Smoke-Test Contract

### Design

The tool stages all build outputs for each artifact into a temporary
directory before publish.  It then invokes the smoke sequence with a
fixed environment contract so the smoke command knows exactly where
to find staged artifacts.

The smoke command can be any executable — a `just` recipe, a Python
module, a compiled binary, etc.  Teams may run additional validation
beyond the minimum.  The exit contract is: exit 0 = pass, non-zero =
fail.

### Smoke enforcement model

The tool enforces smoke in two tiers with different enforcement
mechanisms.  Smoke is **per-artifact** — each artifact in the
manifest has its own smoke sequence.

#### Tier 1: Built-in baseline smoke (tool-enforced)

The built-in baseline smoke always runs for every artifact.  It is
**machine-enforced** by the tool itself.  The minimum validation
standard depends on the artifact kind and its dependencies:

| Artifact kind + deps           | Built-in smoke does                              | Enforcement     |
|--------------------------------|--------------------------------------------------|-----------------|
| `package` (no native deps)    | Consumer compile against staged artifact         | tool-enforced   |
| `package` (with native deps)  | Consumer compile + link + run against staged artifact | tool-enforced   |
| `app`                         | Compile + link + run the app binary              | tool-enforced   |

- **Package without native deps**: baseline compiles a trivial
  generated consumer that imports the staged package.
- **Package with native deps**: baseline compiles, links, and runs a
  trivial generated consumer.  Compile alone is insufficient because
  link-time native dependency resolution is part of the contract.
- **App**: baseline compiles, links, and runs the app binary.  An app
  that does not build and run is not deployable.

The tool determines the package class from the artifact: if
`native_deps` is present and non-empty, the package is native-backed.

**The built-in smoke always runs first**, even when a custom
`smoke_command` is configured.  This guarantees the baseline
regardless of what the custom smoke does.  The per-artifact sequence
is:

1. Built-in baseline smoke (tool-enforced)
2. Custom `smoke_command` (if configured on the artifact)

If either step fails, deploy of that artifact fails.

**`--skip-smoke` escape hatch**: `--skip-smoke` skips **both** tiers
(built-in baseline and custom smoke) for all artifacts.  This is a CI
recovery escape hatch, not a normal workflow.  When `--skip-smoke` is
used, the tool emits a warning:
`warning: --skip-smoke: baseline smoke skipped; deploy proceeding without validation`

Under normal operation (no `--skip-smoke`), the built-in baseline is
mandatory and cannot be selectively bypassed.

#### Tier 2: Custom smoke (author-attested)

When `smoke_command` is configured on an artifact, the tool invokes
it **after** the built-in baseline passes.  Custom smoke is an
**opaque executable** — the tool observes only its exit status (0 =
pass, non-zero = fail).

The tool does **not** verify which validation phases the custom smoke
performed.  The author attests — by providing the command — that it
performs adequate validation for their artifact's public surface.

The built-in baseline already guarantees the minimum; custom smoke
adds artifact-specific coverage on top of that guarantee.

#### What this means in practice

- The minimum standard is always machine-enforced via the built-in
  baseline — it does not depend on the custom smoke.
- Custom smoke is author-responsible.  The tool trusts exit 0 to
  mean "my artifact-specific validation passed."
- Package authors are expected to provide a `smoke_command` when
  their public surface needs stronger integration coverage (FFI
  wrappers, destructors, trait implementations, multi-module APIs).
  The built-in smoke catches packaging failures; an artifact-specific
  smoke catches API contract failures.

| Smoke tier                     | What it validates                                | Who provides it  | How enforced     |
|--------------------------------|--------------------------------------------------|------------------|------------------|
| Built-in baseline              | Packaging, signing, consumer compile/link/run    | `drift deploy`   | Tool-enforced    |
| Custom (`smoke_command`)       | Public API surface, integration semantics        | Artifact author  | Author-attested  |

### Staged environment contract

The smoke command for each artifact receives these environment
variables:

| Variable                  | Description                                             |
|---------------------------|---------------------------------------------------------|
| `DRIFT_STAGE_DIR`        | Root of the staging directory                           |
| `DRIFT_STAGED_PKG`       | Path to the staged `.dmp` artifact (package kind only)  |
| `DRIFT_STAGED_SIG`       | Path to the staged `.dmp.sig` sidecar (package kind only) |
| `DRIFT_STAGED_BIN`       | Path to the staged binary (app kind only)               |
| `DRIFT_STAGED_PKG_ROOT`  | Staged library root (for `--package-root`) — contains `<package>/<version>/` layout |
| `DRIFT_STAGED_INSTALL`   | Staged install directory for this artifact               |
| `DRIFT_STAGED_TRUST`     | Path to staged trust store for smoke verification (see below) |
| `DRIFT_STAGED_DRIFTC`    | Path to `driftc` executable to use for smoke compilation |
| `DRIFT_ARTIFACT_NAME`    | Artifact name from manifest                              |
| `DRIFT_ARTIFACT_VERSION` | Artifact version from manifest                           |
| `DRIFT_ARTIFACT_KIND`    | Artifact kind (`package` or `app`)                       |

The smoke command inherits the caller's environment (PATH, etc.) so
it can find system tools.  The tool invokes `smoke_command` via
direct process execution (no shell interpretation of the argv array).

### Staged trust mechanism for smoke

Consuming a signed package requires trust material — the consumer
must accept the signing key.  During smoke, the staged package is
freshly signed but there is no real consumer trust store to accept it.
Without a standard mechanism, every repo would reinvent trust-store
setup for smoke.

#### The problem with a single-entry trust store

A smoke consumer that imports the staged package may also transitively
load other signed packages from the package root (e.g., `acme.web`
depends on `net.tls`).  A trust store containing only the staged
package's signer would reject those other packages.  The staged trust
must compose with the rest of the signed-package trust environment.

#### Composition model: overlay trust

`drift deploy` generates a **staged trust store** that merges the
staged signer with a baseline trust set:

1. After signing the staged `.dmp`, the tool extracts the public key
   from the signing key.

2. If a baseline trust store exists (via `--trust-store` on the
   `drift deploy` CLI, or `$DRIFT_TRUST_STORE` env var), the tool
   copies it and adds the staged signer as an additional trusted
   entry.  If no baseline trust store is provided, the staged trust
   store contains only the staged signer (sufficient when the smoke
   consumer loads no other signed packages).

3. The merged result is written to a temporary trust store at
   `$DRIFT_STAGED_TRUST` (a `drift/trust.json` file in the staging
   directory).

4. Both built-in default smoke and custom `smoke_command` use
   `$DRIFT_STAGED_TRUST` as the trust store for consumer compilation.
   The built-in smoke passes `--trust-store $DRIFT_STAGED_TRUST`
   automatically.  Custom smoke commands receive it as an env var
   and should pass it to `driftc`.

The composition rule is: **staged trust = baseline trust + staged
signer**.  This means:

- If the package root contains other signed packages (common case),
  the baseline trust store already trusts their signers.  The
  overlay adds trust for the new package being deployed.
- If the smoke consumer loads only the staged package, no baseline
  is needed — the single-entry staged trust is sufficient.
- The tool does not modify the baseline trust store.  The overlay
  is a new file in the staging directory.

#### Separation from consumer trust policy

| Trust context       | What it is                                          | Who owns it         |
|---------------------|-----------------------------------------------------|---------------------|
| Staged trust        | Temporary overlay: baseline + staged signer         | `drift deploy` tool |
| Baseline trust      | Pre-existing trust for packages in the package root | Deploy operator     |
| Consumer trust      | Real consumer-side `drift/trust.json`               | Package consumer    |

Staged trust is scoped to the smoke phase:

- Created in the staging directory, cleaned up with it.
- Never written to the real consumer trust store.
- The deploy operator controls what baseline trust to provide.
- The consumer controls their own trust policy independently.

#### CLI interaction

```
drift deploy --dest /deploy \
             --trust-store /deploy/drift/trust.json \
             ...
```

`--trust-store` tells `drift deploy` where the baseline trust is.
The tool overlays the staged signer onto this baseline to produce
`$DRIFT_STAGED_TRUST`.  If `--trust-store` is omitted and
`$DRIFT_TRUST_STORE` is not set, the staged trust contains only
the staged signer.

The staged trust store uses the same `drift/trust.json` format as
consumer trust, so `driftc --trust-store $DRIFT_STAGED_TRUST` works
with no special smoke-only trust API.

### Smoke command examples

**Per-artifact smoke in a multi-artifact repo**:

```json
{
  "artifacts": [
    {
      "kind": "package",
      "name": "net.tls",
      "smoke_command": ["just", "smoke-net-tls"]
    },
    {
      "kind": "app",
      "name": "tls-tool",
      "smoke_command": ["just", "smoke-tls-tool"]
    }
  ]
}
```

Each artifact's smoke command runs independently.  The `just` recipes
read `$DRIFT_ARTIFACT_NAME` and `$DRIFT_ARTIFACT_KIND` to know which
artifact they are validating.

**Python smoke**:

```json
{
  "smoke_command": ["python3", "tests/smoke.py"]
}
```

The Python command reads `DRIFT_STAGED_DRIFTC`, `DRIFT_STAGED_PKG_ROOT`,
etc. from `os.environ` and uses them to compile/link/run a consumer.

### What the tool enforces

- Built-in baseline smoke always runs per artifact (tool-enforced minimum).
- Custom `smoke_command` runs after baseline, if configured on the artifact.
- Either smoke failure → deploy of that artifact fails — nothing is published.
- Staged trust store generated and passed via `$DRIFT_STAGED_TRUST`.
- Smoke runs against the staged artifact, not source or previously
  published output.
- Staging directory (including staged trust store) is cleaned up on
  failure.

### What the tool does NOT enforce

- What additional validation the smoke command performs beyond the
  minimum standard.
- What language or runner the smoke command is implemented in.
- How long it runs.
- Whether it spawns additional processes beyond the minimum.

---

## 5. Atomic Publish Model

### Pipeline (per artifact)

```
[1] Build       driftc → staged artifact (.dmp or binary)
[2] Sign        drift sign → staged .dmp.sig (package kind only)
[3] Metadata    native_deps + package_deps into .dmp manifest (package kind, done at build)
[4] Assets      copy declared assets into staged install directory
[5] Stage       all outputs in DRIFT_STAGE_DIR
[6] Smoke       run smoke sequence against staged outputs
[7] Publish     atomically move staged outputs to destination
```

Steps 1–5 produce artifacts in a temporary staging directory.  Step 6
validates the staged artifacts.  Step 7 is the only step that writes to
the publish destination.

When building multiple artifacts, the tool processes each artifact
through the full pipeline independently.  Artifact build order respects
intra-manifest dependencies (if `tls-tool` depends on `net.tls`, the
package is built first).

### Publish layout

#### Package artifacts

Packages are published into a package-directory layout with versioned
subdirectories:

```
<dest>/<package>/<version>/
    <package>.dmp
    <package>.dmp.sig
    assets/  (if any)
```

Example:

```
/deploy/net.tls/0.3.0/
    net.tls.dmp
    net.tls.dmp.sig
    assets/
        docs/

/deploy/acme.crypto/0.9.0/
    acme.crypto.dmp
    acme.crypto.dmp.sig
```

There is no `current` symlink.  Downstream shared packages are
version-pinned only.  Consumers must reference an explicit version
path — there is no implicit "latest" pointer that could create an
accidental-upgrade path.

#### App artifacts

Apps are published to a separate destination specified by `--app-dest`:

```
<app-dest>/<app-name>/<version>/
    <app-name>
```

Example:

```
/apps/tls-tool/0.3.0/
    tls-tool
```

Apps are not signed and are not published to the package root.  The
`--app-dest` flag is required when the manifest contains `app`
artifacts and `--dest` is specified for packages.

If the manifest contains only `app` artifacts, `--app-dest` is
required and `--dest` is not needed.  If the manifest contains only
`package` artifacts, `--app-dest` is not needed.

### `--package-root` consumption model

The standard `--package-root` for multi-package consumption points at
the **library root** — the parent directory containing all package
directories:

```
driftc --package-root /deploy consumer.drift
```

The compiler walks `<package-root>/<package>/<version>/` to discover
available packages.  This is the general composable model: one
`--package-root` covers all packages published under that root.

Pointing `--package-root` directly at a single version directory
(e.g., `/deploy/net.tls/0.3.0`) is **not** a supported consumption
path.  The compiler expects the package-root to contain
`<package>/<version>/` subdirectories, not a bare `.dmp` file at
the top level.  If a consumer only needs one package, the
`--package-root` still points at the library root.

Multiple packages under the same `<dest>` do not collide.

### Version selection

When multiple versions of a package exist under the library root,
the consumer must specify which version to use.  Version selection
is expressed via `--dep` flags on the `driftc` command line:

```
driftc --package-root /deploy \
       --dep net.tls@0.3.0 \
       --dep acme.crypto@0.9.0 \
       consumer.drift
```

`--dep <package>@<version>` is a repeatable flag that selects
an exact dependency version for a consumed package.  The compiler
discovers all `.dmp` files under the package root, loads them,
then filters by the dep selection (post-load filtering).

> **Implementation note (0.27.48-dev):** The plan originally
> specified `--package-version` for this flag, but that name
> conflicts with the existing producer-side `--package-version`
> (the SemVer string for `--emit-package`).  The consumer flag
> is `--dep` — short, clearly directional (consumed dependency,
> not produced artifact), and uses `@` for package-version
> association.

Selection rules:

- If `--dep` is specified for a package, the compiler loads
  exactly that version.  If the version is not found among loaded
  packages, compilation fails with a clear error:
  `error: package 'net.tls' version '0.4.0' not found under package roots (available: 0.3.0)`
- If `--dep` is **not** specified for a package and only one
  version exists, the compiler loads it (unambiguous single-version case).
- If `--dep` is not specified and **multiple** versions exist,
  compilation fails with a clear error:
  `error: multiple versions of 'net.tls' found (0.2.0, 0.3.0); use --dep net.tls@<version> to select`
- The compiler never silently picks the "highest" or "newest"
  version.  Ambiguous version state is always an error.

This keeps version selection explicit and predictable.  There is no
implicit resolution, no "latest" heuristic, and no version range
matching at the compiler level.  (Version range matching is a
tool-level concern for `drift deploy` when resolving `package_deps`
during build.)

Rollback is a consumer-side version pin change:

```
# Before: --dep net.tls@0.3.0
# After:  --dep net.tls@0.2.0
```

- If the version directory already exists, it is backed up before
  replacement and restored on failure.
- On failure, the staging directory is cleaned up and no publish occurs.

---

## 6. Tool Interface

### CLI

```
drift deploy [--manifest <path>] [--dest <path>] [--app-dest <path>]
             [--package-root <path>] [--artifact <name>]
             [--driftc <path>]
             [--sign-key-file <path> | --sign-key-cmd <cmd>]
             [--trust-store <path>] [--target <triple>]
             [--skip-smoke] [--dry-run]
```

| Flag              | Default                    | Description                     |
|-------------------|----------------------------|---------------------------------|
| `--manifest`      | `./drift-manifest.json`     | Path to project manifest        |
| `--dest`          | required for packages      | Publish destination root (package artifacts) |
| `--app-dest`      | required for apps          | Publish destination root (app artifacts) |
| `--package-root`  | `--dest` value             | Library root for resolving external `package_deps` (repeatable) |
| `--artifact`      | all                        | Build/deploy only this artifact (repeatable) |
| `--driftc`        | `driftc` from PATH         | Compiler to use for build       |
| `--sign-key-file` | `$DRIFT_SIGN_KEY_FILE`     | Ed25519 signing key file        |
| `--sign-key-cmd`  | `$DRIFT_SIGN_KEY_CMD`      | Command that outputs signing key |
| `--trust-store`   | `$DRIFT_TRUST_STORE`       | Baseline trust store for smoke overlay |
| `--target`        | host triple                | Target triple for artifact build |
| `--update-lock`   | false                      | Re-resolve all `package_deps` and rewrite `drift-lock.json` |
| `--skip-smoke`    | false                      | Skip all smoke (CI escape hatch; emits warning) |
| `--dry-run`       | false                      | Run build + sign + smoke, do not publish |

### Dependency resolution: `--package-root`

When an artifact declares `package_deps`, the tool must resolve those
dependencies to actual `.dmp` packages.  `--package-root` specifies
where the tool looks for external packages:

```
drift deploy --dest /deploy --package-root /deploy consumer-project/
```

Resolution rules:

- `--package-root` is repeatable.  The tool searches all specified
  roots in order.
- If `--package-root` is not specified, the tool defaults to the
  `--dest` value.  This means the publish destination doubles as the
  dependency source — a natural model when all packages are published
  to the same library root.
- Intra-manifest dependencies (where one artifact depends on another
  artifact in the same manifest) are resolved from the staged output,
  not from the package root.  This ensures the build uses the
  just-built artifact, not a previously published version.

This keeps the tool usable with directory-based package roots only.
No central registry or fetch mechanism is needed.

**Separation of publish and dependency concerns**: `--dest` is where
artifacts are published.  `--package-root` is where dependencies are
found.  They default to the same value because the common case is a
single shared library root, but they can be set independently when
the dependency source differs from the publish destination (e.g.,
building against a staging copy of dependencies while publishing to
production).

### Range resolution algorithm

The resolution algorithm converts declared `package_deps` constraints
into exact versions.  It runs inside `drift deploy` — the compiler
(`driftc`) never resolves ranges.

The algorithm is **constraint-aggregation + highest-satisfying-all**.
It is fully order-independent: the same set of constraints and the
same set of available packages always produces the same resolved
graph, regardless of filesystem ordering, `--package-root` ordering,
or `package_deps` array ordering in manifests.

#### Phase 1: Discovery (build package index)

The tool scans each `--package-root` for `.dmp` files (recursive
glob, same as `driftc`).  It loads each `.dmp` header and manifest
to extract `package_id`, `package_version`, and `package_deps`,
building an index:

```
{package_id → [{version, path, sha256_of_dmp_bytes}]}
```

If the same `package_id` + `package_version` pair appears in
multiple roots, the tool keeps the entry from the **first
`--package-root`** in CLI order.  This is the only ordering
dependency in the algorithm, and it is explicit: CLI flag order
is user-controlled and reproducible.  If the same pair appears
twice within a single root (duplicate `.dmp` files), the tool
fails:
`error: duplicate package 'net.tls' version '0.3.2' found in <root>`

#### Phase 2: Constraint aggregation and selection

The algorithm operates on a constraint map:
`{package_id → [constraint_entry]}`, where each constraint entry
records the semver range and its source (which package/artifact
introduced it).

```
1.  Initialize constraint_map from the artifact's direct
    package_deps.  Each entry's source is "artifact '<name>'".

2.  Initialize resolved = {} (package_id → exact version).
    Initialize work_queue = set of package_ids in constraint_map.

3.  While work_queue is non-empty:

    a.  Pop the lexicographically smallest package_id from the
        work_queue.  (Lexicographic ordering is the determinism
        guarantee — it replaces BFS traversal order with a
        content-derived order that is independent of discovery
        or insertion sequence.)

    b.  Collect ALL constraints on this package_id from
        constraint_map.

    c.  Find the highest version in the package index that
        satisfies ALL constraints simultaneously.

    d.  If no version satisfies all constraints → error:
        error: conflicting constraints on 'acme.crypto':
          artifact 'tls-tool' requires acme.crypto ^0.9.0
          net.tls 0.3.2 requires acme.crypto ^1.0.0
          no version satisfies all constraints

    e.  If this package_id is already in resolved:
        - If the resolved version still satisfies all
          constraints (including any newly added ones) → skip,
          no change needed.
        - If the resolved version no longer satisfies → this
          means a later transitive constraint invalidated an
          earlier selection.  The tool fails with the same
          conflict error as (d).  (This cannot happen in a
          correctly converging run because we aggregate before
          selecting, but it is the safety check.)

    f.  Record: resolved[package_id] = selected version.

    g.  Load the selected .dmp's package_deps.  For each
        transitive dep:
        - Add a constraint entry to constraint_map (source:
          "<package_id> <version>").
        - If the target package_id is not yet in resolved,
          add it to work_queue.
        - If the target package_id IS already in resolved,
          verify that the resolved version satisfies the new
          constraint.  If not → conflict error.  If yes →
          skip (no re-resolution needed).

4.  Return resolved.
```

**Conflict is a hard build failure**.  If resolution fails at any
step (d, e, or g), the artifact build stops immediately.  No
lockfile is written or partially updated.  No `--dep` flags are
emitted.  No `driftc` invocation occurs — codegen and linking
never start for an artifact with an unresolved or conflicting
dependency graph.  This is not a warning, not a deferred
runtime/link-time check, and not a partial result.  The tool
exits non-zero with the conflict diagnostic before any compiler
work begins for that artifact.

**Why this is deterministic**: the only ordering choice is step 3a
(lexicographic pop from work_queue).  All other operations — index
lookup, constraint satisfaction, version comparison — are pure
functions of their inputs.  Two runs with the same package index
and the same `drift-manifest.json` will always produce the same
resolved map.

**Why "aggregate then select" instead of "first-seen-wins"**: with
first-seen-wins, the traversal order determines the outcome.  If
package A's constraint selects `foo 1.3.0` and package B's constraint
would allow `foo 1.5.0`, the result depends on whether A or B is
processed first.  By aggregating all constraints and selecting the
highest version satisfying all of them, the result is independent of
processing order.  This also means a later transitive constraint
cannot produce a *higher* version than one that would have been
selected otherwise — the aggregation only narrows the candidate set,
never widens it.

**No implicit "latest" heuristic beyond constraint satisfaction**:
the tool selects the highest version *that satisfies all accumulated
constraints*.  If only one version satisfies, it is selected
regardless of whether it is the "latest."  If multiple satisfy, the
highest is chosen — this matches npm/cargo/pub behavior and is
appropriate because the constraint already encodes compatibility
intent, and highest-within-constraint gets the latest compatible
bugfixes.  The lock file pins the exact selection; re-resolution
only happens on explicit `--update-lock`.

**The MVP does not attempt split-version resolution** (diamond
dependencies at different major versions coexisting).  This is a
future extension if the ecosystem grows large enough to need it.

**Per-artifact resolution**: each artifact in the manifest gets its
own resolved graph.  Two artifacts in the same project may depend on
different versions of the same package (they are independent build
units).  The lock file records one section per artifact.

### Resolved dependency recording: `drift-lock.json`

After resolving all `package_deps` constraints, the tool writes a
lock file recording the exact versions selected.  This makes the
resolved dependency graph reproducible without re-resolving from
the package root.

Lock file location: `drift-lock.json` next to `drift-manifest.json`.

```json
{
  "schema_version": 1,
  "artifacts": {
    "net.tls": {
      "resolved": {
        "acme.crypto": {
          "version": "0.9.0",
          "integrity": "sha256:<hex>",
          "dep_type": "direct"
        }
      }
    },
    "tls-tool": {
      "resolved": {
        "net.tls": {
          "version": "0.3.2",
          "integrity": "sha256:<hex>",
          "dep_type": "direct"
        },
        "acme.crypto": {
          "version": "0.9.0",
          "integrity": "sha256:<hex>",
          "dep_type": "transitive"
        }
      }
    }
  }
}
```

| Field        | Description                                               |
|--------------|-----------------------------------------------------------|
| `version`    | Exact resolved version                                    |
| `integrity`  | `"sha256:<hex>"` — SHA-256 of the raw `.dmp` file bytes   |
| `dep_type`   | `"direct"` or `"transitive"` — origin of the dependency  |

#### Integrity model

The `integrity` field is the hex-encoded SHA-256 digest of the
**entire `.dmp` file contents** (the raw bytes on disk, not the
manifest JSON or any extracted sub-object).  This is the same
object that Ed25519 signatures cover, so integrity verification
and signature verification agree on what "the package" is.

**How the tool finds the artifact to verify**: on lock load, the
tool scans `--package-root` directories (same discovery as
resolution), building the package index `{package_id → [{version,
path, sha256}]}`.  For each lock entry, it looks up `package_id`
+ `version` in the index.  If found, it compares the index's
`sha256` (computed during discovery) against the lock's
`integrity` value.  If they differ:
`error: locked dependency 'net.tls' integrity mismatch (expected sha256:abc..., got sha256:def...)`

**Duplicate package_id + version across roots**: the package
index keeps only the entry from the **first `--package-root`** in
CLI order (same rule as resolution; see Phase 1 above).  The lock
integrity is verified against whichever `.dmp` the index selects.
If a team changes which root provides a package, a hash mismatch
will surface — this is intentional, because the `.dmp` bytes may
differ even if the version string is the same.

**No source path in the lock file**: `source` (absolute path) is
deliberately not recorded.  The lock file is version-controlled
and shared across machines; absolute paths are machine-local and
would cause spurious diffs.  The tool re-discovers the `.dmp` by
scanning `--package-root` for the matching `package_id` +
`version` pair at build time.

`dep_type` is informational: it lets humans and tools understand
why a package is in the graph.  It does not affect resolution.

Lock file behavior:

- **If `drift-lock.json` exists**: the tool uses it as the
  authoritative resolution.  For each artifact, it loads the exact
  versions listed.  If a listed version is no longer present in any
  package root, the tool fails:
  `error: locked dependency 'net.tls' version '0.3.2' not found under package roots`
  If the `.dmp` hash does not match the `integrity` value, the
  tool fails:
  `error: locked dependency 'net.tls' integrity mismatch (expected sha256:abc..., got sha256:def...)`
- **If `drift-lock.json` does not exist**: the tool resolves from
  the package root (highest matching version per constraint),
  writes `drift-lock.json`, and proceeds.
- **`--update-lock`**: re-resolves all `package_deps` from the
  package root, ignoring the existing lock file, and writes a new
  `drift-lock.json`.  Use after bumping constraints in
  `drift-manifest.json` or after publishing new dependency versions.
- **Lock file is checked into version control**: it is a project
  artifact, not a build cache.  CI builds use the lock file to get
  deterministic builds.
- **Lock–manifest consistency**: on load, the tool verifies that
  every direct `package_deps` entry declared in the manifest has a
  corresponding entry in the lock file.  If the manifest adds a new
  dependency not present in the lock, the tool fails:
  `error: dependency 'foo' declared in drift-manifest.json but not present in drift-lock.json; run drift deploy --update-lock`
  This prevents partial locks from silently passing.

The lock file records only Drift package dependencies, not native
deps (native library versions are system state, not resolvable by
the tool).

This gives downstream consumers three ways to get the same graph:

1. **Check in `drift-lock.json`**: deterministic, zero re-resolution.
2. **Re-resolve with `--update-lock`**: pick up new compatible
   versions, produce new lock file.
3. **Delete `drift-lock.json`**: fresh resolution from current
   package root state.

### `--dep` expansion: lock → compiler

The lock file does not feed into the compiler directly.  Instead,
`drift deploy` expands the resolved graph into repeated `--dep`
flags on the `driftc` command line:

```
# drift deploy reads drift-lock.json for artifact 'tls-tool' and expands:
driftc --package-root /deploy \
       --dep net.tls@0.3.2 \
       --dep acme.crypto@0.9.0 \
       --stdlib-root /deploy/stdlib \
       -o build/tls-tool \
       src/main.drift
```

This is a mechanical expansion: for each entry in the artifact's
`resolved` map, emit `--dep <name>@<version>`.  The compiler's
existing `--dep` selection logic (post-load filtering) ensures
exactly those versions are used.

**Why expansion, not direct lock consumption by `driftc`**:

- The compiler should not contain lock-file parsing or range
  resolution logic.  It is a compiler, not a package manager.
- `--dep` is the stable, minimal interface between the tool and
  the compiler.  The lock file format can evolve independently.
- Users who do not use `drift deploy` can still pass `--dep` flags
  directly — the compiler does not require a lock file.
- The expansion is fully inspectable: the `[driftc] link:` stderr
  line and `--dep` flags are visible in build logs.

**Future extension**: if the `--dep` flag list becomes unwieldy for
very large dependency graphs, a later enhancement could add
`--dep-file <path>` to `driftc` — a file containing one
`<name>@<version>` per line.  This is additive and does not change
the contract.

### User-facing workflow

The concrete intended flow for a downstream project:

```
1. Author writes drift-manifest.json
   ┌─────────────────────────────────────────┐
   │ {                                       │
   │   "project": { "name": "my-app" },      │
   │   "artifacts": [{                       │
   │     "name": "my-app",                   │
   │     "version": "1.0.0",                 │
   │     "kind": "app",                      │
   │     "package_deps": [                   │
   │       {"name": "net.tls", "version": "^0.3.0"}, │
   │       {"name": "web", "version": "^1.0.0"}      │
   │     ]                                   │
   │   }]                                    │
   │ }                                       │
   └─────────────────────────────────────────┘
   This expresses declared intent: "I need tls ≥0.3.0 <0.4.0
   and web ≥1.0.0 <2.0.0".  These are constraints, not exact
   versions.

2. First drift deploy resolves constraints
   $ drift deploy --dest /deploy --package-root /deploy .

   The tool scans /deploy for .dmp packages, finds:
     net.tls 0.3.2, net.tls 0.3.4, web 1.0.0, web 1.1.0
   Resolves: net.tls → 0.3.4 (highest ^0.3.0), web → 1.1.0
   Writes drift-lock.json recording the exact graph.

3. drift-lock.json is checked into version control
   $ git add drift-lock.json && git commit

   From this point, all team members and CI get the same
   exact dependency versions.

4. Subsequent builds reuse the lock
   $ drift deploy --dest /deploy --package-root /deploy .

   The tool reads drift-lock.json, verifies integrity, and
   passes --dep net.tls@0.3.4 --dep web@1.1.0 to driftc.
   No re-resolution.  Same graph, same binary.

5. Updating dependencies
   $ drift deploy --update-lock --dest /deploy --package-root /deploy .

   The tool ignores the existing lock, re-resolves from the
   package root (maybe net.tls 0.3.5 was published), writes a
   new drift-lock.json.  Author reviews the diff and commits.
```

**Where declared intent ends and exact resolved graph begins**:

| Layer                  | Contains                              | Who writes it         |
|------------------------|---------------------------------------|-----------------------|
| `drift-manifest.json`   | Version range constraints (`^0.3.0`)  | Project author        |
| `drift-lock.json`      | Exact resolved versions (`0.3.4`)     | `drift deploy` tool   |
| `--dep` flags          | Exact versions passed to compiler     | `drift deploy` tool   |
| `.dmp` manifest        | Declared constraints of the package   | Package producer       |
| App sidecar `.meta.json` | Exact graph used to build the binary | `drift deploy` tool   |

The author declares constraints.  The tool resolves them once and
records the exact graph.  The compiler consumes exact versions only.
There is never ambiguity about which version was used.

### App provenance sidecar

After building an app artifact, `drift deploy` writes a sidecar
metadata file alongside the binary:

```
<app-dest>/<app>/<version>/
  <app>
  <app>.meta.json
```

The sidecar records the exact resolved graph used to build the app:

```json
{
  "schema_version": 1,
  "app": "my-app",
  "version": "1.0.0",
  "target": "x86_64-linux-gnu",
  "compiler_version": "0.27.48-dev",
  "built_at": "2026-03-14T18:30:00Z",
  "resolved_deps": {
    "net.tls": {
      "version": "0.3.4",
      "integrity": "sha256:<hex>"
    },
    "web": {
      "version": "1.1.0",
      "integrity": "sha256:<hex>"
    },
    "acme.crypto": {
      "version": "0.9.0",
      "integrity": "sha256:<hex>"
    }
  }
}
```

This is the operational/audit artifact.  Given a published app, an
operator can inspect `<app>.meta.json` to see exactly what package
graph the binary was built against — not just declared constraints,
but exact consumed versions with integrity hashes.

The sidecar is written from the same resolved graph used for `--dep`
expansion, so it is guaranteed to match the actual build.

**MVP scope**: the sidecar is emitted by `drift deploy` for app
artifacts.  It is not embedded in the binary.  Binary-embedded
provenance is a later enhancement for single-file interrogation.

**Package artifacts do not get a sidecar**: their dependencies are
already recorded in the signed `.dmp` manifest (`package_deps`) and
in `drift-lock.json`.  The sidecar is specifically for apps, which
are unsigned binaries without manifest metadata.

### Artifact selection

By default, `drift deploy` builds and deploys **all** artifacts in
the manifest.  The `--artifact` flag selects specific artifacts:

```
# Deploy only net.tls:
drift deploy --dest /deploy --artifact net.tls

# Deploy two specific artifacts:
drift deploy --dest /deploy --app-dest /apps \
             --artifact net.tls --artifact tls-tool
```

If `--artifact` names an artifact not in the manifest, the tool fails:
`error: artifact 'foo' not found in drift-manifest.json`

### Manifest vs CLI responsibility

`drift-manifest.json` is the **full authoritative build and deploy
manifest** for the project.  It is not a partial hint or supplement
to external build logic — it is the complete definition of what gets
built, what it depends on, and how it is validated.  The tool reads
`drift-manifest.json` and produces deployable artifacts without
requiring any external build configuration (Makefile, justfile,
script) to supply identity, source inputs, or dependency information.

The CLI supplies **operational overrides** — things that vary per
invocation (where to publish, what key to sign with, what target to
build for, where to find dependencies) but do not change the
project/artifact definition itself.

| Concern                        | Source                        | Where specified                |
|--------------------------------|-------------------------------|-------------------------------|
| Project identity               | Manifest (authoritative)      | `project.name`                |
| Project license                | Manifest (authoritative)      | `project.license`             |
| Artifact identity (kind, name) | Manifest (authoritative)      | per-artifact `kind`, `name`   |
| Artifact version               | Manifest (authoritative)      | per-artifact `version`        |
| Source inputs                  | Manifest (authoritative)      | per-artifact `entry_module`, `modules` |
| Package dependencies           | Manifest (authoritative)      | per-artifact `package_deps`   |
| Native dependency declarations | Manifest (authoritative)      | per-artifact `native_deps`    |
| Smoke command                  | Manifest (authoritative)      | per-artifact `smoke_command`  |
| Assets                         | Manifest (authoritative)      | per-artifact `assets`         |
| Artifact build invocation      | Tool-owned                    |                               |
| Signing workflow               | Tool-owned                    |                               |
| Staging layout                 | Tool-owned                    |                               |
| Smoke env contract             | Tool-owned                    |                               |
| Staged trust store for smoke   | Tool-owned                    |                               |
| Atomic publish                 | Tool-owned                    |                               |
| Package publish destination    | CLI (operational)             | `--dest`                      |
| App publish destination        | CLI (operational)             | `--app-dest`                  |
| Dependency source roots        | CLI (operational)             | `--package-root` (default: `--dest`) |
| Artifact selection             | CLI (operational)             | `--artifact`                  |
| Target triple                  | CLI (operational)             | `--target` or derived         |
| Signing key source             | CLI (operational)             | `--sign-key-file` / env       |
| Baseline trust for smoke       | CLI (operational)             | `--trust-store` / env         |
| Consumer trust policy          | Consumer-side                 | Not in tool or manifest       |

---

## 7. What Remains Artifact-Specific vs Standardized

### Standardized by the tool

- Build invocation (`driftc` with correct flags per artifact kind)
- Signing workflow (Ed25519, detached `.dmp.sig`, package kind only)
- Native dep metadata location (inside `.dmp` manifest)
- Package dep metadata location (inside `.dmp` manifest)
- Staged smoke env contract (`DRIFT_STAGE_DIR`, etc.)
- Publish layout (`<dest>/<package>/<version>/` for packages,
  `<app-dest>/<app>/<version>/` for apps, version-pinned only)
- Asset staging and publish (`assets/` subdirectory in install root)
- Metadata emission (native deps, package deps, version, target in `.dmp` manifest)
- Multi-artifact build ordering from manifest dependency graph

### Remains artifact-specific

- What libraries to declare (author's `native_deps` list)
- What packages to depend on (author's `package_deps` list)
- What the smoke command does beyond the minimum (author's `smoke_command`)
- Source file organization (declared in manifest `entry_module` / `modules`)
- Consumer-side trust store configuration
- System-level library installation (apt/yum/brew)
- Library search paths at consumption time (`--link-search`)

---

## 8. Diagnostics

### Link-time enrichment

When the linker fails and the error references `-l<name>` for a library
declared in a loaded package's `native_deps`, `driftc` emits:

```
error: linker failed
hint: package 'net.tls' (v0.3.0) requires native library 'ssl' (-lssl).
      Install the development package for your distribution, or pass
      --link-search <dir> to specify the library location.
```

### Deploy-time diagnostics

| Condition                              | Diagnostic                                          |
|----------------------------------------|-----------------------------------------------------|
| Manifest missing or invalid JSON       | `error: drift-manifest.json not found / parse error`  |
| `schema_version` missing               | `error: drift-manifest.json missing required field 'schema_version'` |
| `schema_version` unsupported           | `error: drift-manifest.json schema_version <N> is not supported by this version of drift deploy` |
| Required field missing                 | `error: artifact 'net.tls': missing required field 'entry_module'` |
| Duplicate artifact name                | `error: duplicate artifact name 'net.tls' in drift-manifest.json` |
| Unknown artifact kind                  | `error: artifact 'foo': unknown kind 'service'`     |
| `--artifact` not in manifest           | `error: artifact 'foo' not found in drift-manifest.json` |
| Package depends on app                 | `error: package 'net.tls' cannot depend on app 'tls-tool'` |
| `--dest` missing with package artifacts | `error: --dest required (manifest contains package artifacts)` |
| `--app-dest` missing with app artifacts | `error: --app-dest required (manifest contains app artifacts)` |
| Signing key not available              | `error: signing key required (--sign-key-file or DRIFT_SIGN_KEY_FILE)` |
| Declared asset path missing            | `error: artifact 'net.tls': declared asset not found: docs/missing.md` |
| Package build fails                    | Forward driftc diagnostics                           |
| Smoke command fails (non-zero exit)    | `error: artifact 'net.tls': smoke command failed (exit <N>); not publishing` |
| Smoke command not specified            | `note: artifact 'net.tls': no smoke_command configured; running built-in default smoke` |
| Publish destination not writable       | `error: cannot write to <dest>`                      |
| Locked version not found               | `error: locked dependency 'net.tls' version '0.3.2' not found under package roots` |
| Locked integrity mismatch              | `error: locked dependency 'net.tls' integrity mismatch (expected sha256:abc..., got sha256:def...)` |
| Lock file written                      | `note: wrote drift-lock.json (3 packages resolved)` |
| Lock missing declared dep              | `error: dependency 'foo' declared in drift-manifest.json but not present in drift-lock.json; run drift deploy --update-lock` |
| Dependency unsatisfied                 | `error: artifact 'tls-tool': package dependency 'net.tls ^0.3.0' not satisfied (searched: /deploy)` |
| Transitive conflict                    | `error: conflicting constraints on 'acme.crypto': net.tls requires ^0.9.0, web requires ^1.0.0` |
| Unknown `native_deps.schema_version`   | `warning: native_deps schema_version <N> is newer than supported; some metadata may be ignored` |

---

## 9. Comparison with Current Deploy Pipeline

The existing compiler/stdlib deploy pipeline is the compiler distribution
deploy.  It is NOT replaced by `drift deploy`.  The two serve different
purposes:

| Aspect           | Compiler deploy pipeline            | `drift deploy` (new)           |
|------------------|-------------------------------------|--------------------------------|
| What it deploys  | Compiler + stdlib + runtime         | Downstream user packages + apps |
| PEX build        | yes                                 | no                             |
| Runtime archives | yes                                 | no                             |
| Signing          | stdlib signing                      | user package signing           |
| Smoke            | Hardcoded consumer compile          | Author-specified per artifact  |
| Publish          | Flat `drift-<ver>+abi<N>/`          | `<pkg>/<ver>/` package-dir     |
| Multi-artifact   | no (single pipeline)                | yes (per-artifact in manifest) |
| Who uses it      | Drift maintainers                   | Downstream project authors     |

`drift deploy` standardizes what downstream project authors do.  It
inherits the staging → smoke → atomic publish pattern from the compiler
deploy pipeline but makes it generic, manifest-driven, and
multi-artifact.

---

## 10. Rollout Order

### Phase 1: MVP

Deliverables:

1. **`drift-manifest.json` schema** — schema_version, project (name,
   license), artifacts array with kind, name, version, description,
   entry_module, modules, package_deps, native_deps, assets,
   smoke_command.  Single authoritative source of truth for project
   identity, per-artifact build inputs, and deploy metadata.
   Validator in Python.

2. **Artifact kinds** — `package` and `app` with distinct build,
   signing, and publish behavior.

3. **`package_deps` in `.dmp` manifest** — `driftc --emit-package`
   accepts `--package-dep` and writes `package_deps` section into
   manifest.

4. **`native_deps` in `.dmp` manifest** — `driftc --emit-package`
   accepts `--native-link-lib` and writes `native_deps` section into
   manifest.

5. **Consumer auto-link** — `driftc` reads `native_deps` from loaded
   packages, appends `-l<name>` to link command.
   `--no-package-native-deps` opt-out.

6. **Link-time diagnostic enrichment** — enrich linker failures with
   package-declared library hints.

7. **`drift deploy` tool** — reads `drift-manifest.json`, orchestrates
   per-artifact build → sign → asset copy → stage → smoke → publish.
   Fixed smoke env contract.  Asset staging into `assets/` subdirectory.
   `--artifact` selection.  Artifact build ordering from dependency graph.

8. **Smoke env contract** — `DRIFT_STAGE_DIR`, `DRIFT_STAGED_PKG`,
   `DRIFT_STAGED_SIG`, `DRIFT_STAGED_BIN`, `DRIFT_STAGED_PKG_ROOT`,
   `DRIFT_STAGED_INSTALL`, `DRIFT_STAGED_TRUST` (overlay: baseline +
   staged signer), `DRIFT_STAGED_DRIFTC`, `DRIFT_ARTIFACT_NAME`,
   `DRIFT_ARTIFACT_VERSION`, `DRIFT_ARTIFACT_KIND`.

9. **Tests** — see §11.

10. **Version bump** — compiler version bump for `--native-link-lib`,
    `--package-dep`, and consumer auto-link.  No ABI bump.

### Phase 2: future extensions

- `pkg_config` integration for automatic flag discovery
- `platform`-conditional library names
- `required` field on native deps (optional/conditional deps with availability probing)
- `min_version` on native deps
- `pre_build_command` in manifest
- `consumers` field for automatic consumer compile tests
- `drift deploy publish --registry <url>` for registry-based distribution
- Manifest `extends` for shared base configuration
- Additional artifact kinds (e.g., `service`, `plugin`)

---

## 11. Test Strategy

### Compiler-level tests (`test_driftc_package_v0.py`)

| Test                                             | What it validates                                    |
|--------------------------------------------------|------------------------------------------------------|
| `test_native_deps_manifest_roundtrip`            | Build package with `--native-link-lib`, load it, verify `native_deps` in manifest |
| `test_native_deps_absent_is_empty`               | Load package without `native_deps` — defaults to empty list |
| `test_native_deps_consumer_auto_link`            | Package declares `ssl`, consumer link command includes `-lssl` |
| `test_native_deps_multi_package_merge`           | Two packages contribute different libs; both appear in link command |
| `test_native_deps_dedup`                         | Two packages both declare `ssl`; only one `-lssl` |
| `test_native_deps_with_cli_override`             | Consumer `--link-lib` coexists with package-declared libs |
| `test_native_deps_opt_out`                       | `--no-package-native-deps` suppresses auto-linking |
| `test_native_deps_missing_lib_diagnostic`        | Linker failure includes enriched hint with package name and library |
| `test_package_deps_manifest_roundtrip`           | Build package with `--package-dep`, load it, verify `package_deps` in manifest |
| `test_package_deps_absent_is_empty`              | Load package without `package_deps` — defaults to empty list |
| `test_package_version_pin_selects`               | `--dep net.tls@0.3.0` loads exactly that version |
| `test_package_version_missing_fails`             | `--dep` for nonexistent version → clear error |
| `test_package_version_ambiguous_fails`           | Multiple versions, no `--dep` → clear error listing versions |
| `test_package_version_single_unambiguous`        | Single version present, no `--dep` → loads it |
| `test_dep_malformed_rejected`                    | `--dep net.tls` (no `@VERSION`) → rejected |
| `test_dep_duplicate_rejected`                    | `--dep` specified twice for same package → rejected |

### Deploy tool tests

| Test                                             | What it validates                                    |
|--------------------------------------------------|------------------------------------------------------|
| `test_deploy_manifest_parse`                     | Valid and invalid `drift-manifest.json` parsing       |
| `test_deploy_manifest_multi_artifact`            | Manifest with multiple artifacts parses correctly    |
| `test_deploy_manifest_schema_version_missing`    | Missing `schema_version` → clear error               |
| `test_deploy_manifest_schema_version_unsupported`| `schema_version` > supported → clear error, no fallback |
| `test_deploy_manifest_duplicate_name`            | Duplicate artifact name → clear error                |
| `test_deploy_manifest_package_depends_on_app`    | Package with package_dep referencing app → clear error |
| `test_deploy_build_and_stage_package`            | Package artifact produces staged `.dmp` + `.dmp.sig` |
| `test_deploy_build_and_stage_app`                | App artifact produces staged binary                  |
| `test_deploy_artifact_selector`                  | `--artifact net.tls` builds only that artifact       |
| `test_deploy_artifact_build_order`               | Intra-manifest deps built before dependents          |
| `test_deploy_package_root_resolves_dep`          | `--package-root` resolves external `package_deps`    |
| `test_deploy_package_root_defaults_to_dest`      | No `--package-root` → uses `--dest` as dependency source |
| `test_deploy_package_root_unresolved_dep_fails`  | Unsatisfied `package_deps` constraint → clear error  |
| `test_deploy_intra_manifest_dep_uses_staged`     | Intra-manifest dep resolves from staged output, not package root |
| `test_deploy_lock_file_written`                  | First deploy writes `drift-lock.json` with resolved versions and integrity |
| `test_deploy_lock_file_deterministic`            | Subsequent deploy with lock file uses exact locked versions |
| `test_deploy_lock_file_missing_version_fails`    | Locked version no longer in package root → clear error |
| `test_deploy_lock_file_integrity_mismatch_fails` | `.dmp` hash changed since lock → clear error |
| `test_deploy_update_lock_re_resolves`            | `--update-lock` ignores existing lock and picks highest matching |
| `test_deploy_lock_file_transitive`               | Transitive deps recorded in lock file with `dep_type: "transitive"` |
| `test_deploy_lock_manifest_consistency`          | New manifest dep not in lock → error with `--update-lock` hint |
| `test_deploy_lock_per_artifact`                  | Two artifacts with different deps → separate resolved sections |
| `test_deploy_resolution_highest_compatible`      | Multiple versions satisfy `^0.3.0` → highest selected |
| `test_deploy_resolution_unsatisfied_fails`       | No version satisfies constraint → clear error |
| `test_deploy_resolution_transitive_conflict`     | Conflicting transitive constraints → clear error listing both |
| `test_deploy_dep_expansion`                      | Lock file entries expand to `--dep` flags on driftc invocation |
| `test_deploy_app_sidecar_written`                | App artifact produces `<app>.meta.json` with resolved graph |
| `test_deploy_app_sidecar_matches_lock`           | Sidecar `resolved_deps` matches lock file entries |
| `test_deploy_smoke_pass_publishes`               | Smoke exit 0 → artifacts appear at destination      |
| `test_deploy_smoke_fail_no_publish`              | Smoke exit 1 → destination unchanged                |
| `test_deploy_smoke_env_contract`                 | Smoke command receives all `DRIFT_STAGED_*` and `DRIFT_ARTIFACT_*` vars |
| `test_deploy_smoke_env_kind_specific`            | `DRIFT_STAGED_PKG` set for package, `DRIFT_STAGED_BIN` set for app |
| `test_deploy_staged_trust_valid`                 | Staged trust store accepts the signing key used for this deploy |
| `test_deploy_staged_trust_overlay`               | Staged trust merges baseline trust + staged signer; consumer can load both |
| `test_deploy_staged_trust_no_baseline`           | No `--trust-store` → staged trust contains only staged signer |
| `test_deploy_staged_trust_cleanup`               | Staged trust store is removed on both success and failure |
| `test_deploy_atomic_publish_package`             | Package version directory published atomically       |
| `test_deploy_atomic_publish_app`                 | App version directory published atomically           |
| `test_deploy_native_deps_in_staged_pkg`          | Staged `.dmp` contains `native_deps` from artifact config |
| `test_deploy_package_deps_in_staged_pkg`         | Staged `.dmp` contains `package_deps` from artifact config |
| `test_deploy_baseline_smoke_compile`             | Built-in baseline smoke compiles consumer (package)  |
| `test_deploy_baseline_smoke_native_link_run`     | Native-backed package → baseline smoke compiles + links + runs |
| `test_deploy_baseline_smoke_app`                 | App → baseline smoke compiles + links + runs binary  |
| `test_deploy_baseline_before_custom`             | Built-in baseline runs before custom `smoke_command`; baseline failure blocks custom |
| `test_deploy_custom_smoke_after_baseline`        | Custom `smoke_command` runs after baseline passes    |
| `test_deploy_assets_staged`                      | Declared assets appear in `$DRIFT_STAGED_INSTALL/assets/` |
| `test_deploy_assets_published`                   | Assets appear in published version directory         |
| `test_deploy_assets_missing_path_fails`          | Missing declared asset → clear error, no publish     |
| `test_deploy_assets_preserves_structure`         | Subdirectory structure preserved under `assets/`     |
| `test_deploy_dry_run`                            | `--dry-run` builds and smokes but does not publish  |
| `test_deploy_skip_smoke_warning`                 | `--skip-smoke` emits warning and skips both tiers    |
| `test_deploy_license_inheritance`                | Artifact without `license` inherits `project.license` |
| `test_deploy_license_override`                   | Artifact with `license` overrides `project.license`  |
| `test_deploy_dest_required_for_packages`         | `--dest` missing with package artifacts → clear error |
| `test_deploy_app_dest_required_for_apps`         | `--app-dest` missing with app artifacts → clear error |

---

## 12. MVP / Non-MVP Boundary

| Feature                                      | MVP | Future |
|----------------------------------------------|-----|--------|
| `drift-manifest.json` manifest (authoritative) | yes |        |
| Project-level `name`, `license`              | yes |        |
| `artifacts` array with per-artifact config   | yes |        |
| Artifact kinds: `package`, `app`             | yes |        |
| Per-artifact `name`, `version`, `description`| yes |        |
| Per-artifact `entry_module`, `modules`       | yes |        |
| Per-artifact `package_deps` with semver range| yes |        |
| Per-artifact `native_deps` with `lib`        | yes |        |
| Per-artifact `smoke_command`                 | yes |        |
| `license` inheritance (project → artifact)   | yes |        |
| `native_deps` in `.dmp` manifest            | yes |        |
| `package_deps` in `.dmp` manifest            | yes |        |
| `--native-link-lib` on `driftc`              | yes |        |
| `--package-dep` on `driftc`                  | yes |        |
| Consumer auto-link from `.dmp`               | yes |        |
| `--no-package-native-deps` opt-out           | yes |        |
| Link-time diagnostic enrichment              | yes |        |
| Staged smoke env contract                    | yes |        |
| Atomic publish, version-pinned only          | yes |        |
| Built-in baseline smoke (tool-enforced)      | yes |        |
| Custom smoke as author-attested tier 2       | yes |        |
| `--dep` consumer dependency version selection | yes |        |
| Staged trust store for smoke                 | yes |        |
| `--artifact` selector                        | yes |        |
| `--package-root` dependency resolution       | yes |        |
| `drift-lock.json` resolved dependency graph  | yes |        |
| `--update-lock` re-resolution               | yes |        |
| Range resolution (highest compatible)        | yes |        |
| Transitive dependency resolution             | yes |        |
| Transitive conflict detection                | yes |        |
| Lock–manifest consistency check              | yes |        |
| Lock → `--dep` expansion                    | yes |        |
| Per-artifact resolution                      | yes |        |
| App provenance sidecar `.meta.json`          | yes |        |
| `--app-dest` for app publish                 | yes |        |
| `--dry-run`                                  | yes |        |
| `--skip-smoke`                               | yes |        |
| `assets` in manifest and publish layout      | yes |        |
| `--dep-file` for large dep graphs           |     | yes    |
| Split-version (diamond) dependency resolution |     | yes    |
| Binary-embedded provenance                   |     | yes    |
| `pkg_config` integration                     |     | yes    |
| `platform`-conditional library names         |     | yes    |
| `required` field / optional native deps       |     | yes    |
| `min_version` on native deps                 |     | yes    |
| `pre_build_command`                          |     | yes    |
| `consumers` auto-test field                  |     | yes    |
| Registry-based publish                       |     | yes    |
| `extends` for shared base manifests          |     | yes    |
| App binary signing                           |     | yes    |
| Additional artifact kinds                    |     | yes    |

---

## 13. Files to Change (MVP)

| File / Location                               | Change                                                    |
|-----------------------------------------------|-----------------------------------------------------------|
| `lang/driftc/driftc.py`                       | `--native-link-lib` arg; `--package-dep` arg; `--dep` arg; manifest emission; consumer auto-link; diagnostic enrichment; `--no-package-native-deps` |
| `lang/driftc/packages/dmir_pkg_v0.py`         | `NativeDepEntry`, `PackageDepEntry` dataclasses; parse from manifest; add to `LoadedPackage` |
| `lang/driftc/packages/provider_v0.py`         | Pass through `native_deps` and `package_deps` in load path |
| `tools/drift_deploy/`                         | New tool: manifest parser, orchestrator, smoke runner, publisher |
| `tools/drift_deploy/drift_deploy.py`          | Main entry point, artifact selection, build ordering       |
| `tools/drift_deploy/manifest.py`              | `drift-manifest.json` schema + validator (project + artifacts) |
| `tools/drift_deploy/lockfile.py`              | `drift-lock.json` read/write/verify, integrity check      |
| `tools/drift_deploy/resolver.py`              | Range resolution, transitive walk, conflict detection, `--dep` expansion |
| `tools/drift_deploy/sidecar.py`               | App provenance sidecar `.meta.json` emission              |
| `tools/drift_deploy/staged_trust.py`          | Staged trust store generation and cleanup                 |
| `lang/tests/driver/test_driftc_package_v0.py` | Native dep and package dep compiler-level tests           |
| `lang/tests/driver/test_drift_deploy.py`      | Deploy tool integration tests (multi-artifact)            |
| `docs/history.md`                             | Changelog entry                                           |
| `lang/driftc/driftc_versions.py`              | Version bump                                              |
