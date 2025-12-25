# Drift tooling, build, and package ecosystem – hybrid specification

This document describes the **Drift toolchain surface** and the machinery behind it:
- project and target configuration
- builds and outputs
- package acquisition, caching, and vendoring
- repositories and discovery (including `drift-sources.json`)
- trust model defaults (TOFU + reserved namespaces)

It is intentionally *hybrid*: part specification, part operational contract.

---

## 1. Goals and non-goals

### Goals
- Deterministic builds
- **Zero implicit network access during compilation**
- Clear separation between *building* and *dependency acquisition*
- Minimal overhead for “hello world”
- Natural scaling to multi-package, multi-binary repositories
- Eliminate “publish before use” friction for **local** packages
- Community-scale discovery without requiring a single central storage service

### Non-goals
- IDE behavior specification
- Test runners or execution environments
- Runtime deployment conventions
- Rich visibility lattice (`pub(crate)` etc.)

---

## 2. Tools and responsibilities

### 2.1 `driftc` (compiler / builder)

`driftc` is responsible for:
- Loading project and target configuration
- Discovering targets
- Resolving modules from **local package roots only**
- Verifying package integrity (hashes) and, when present, package signatures and trust policy **before** using any package contents (**gatekeeper**)
- Compiling sources
- Producing final artifacts (executables or package artifacts)

**Hard rule:** `driftc` never performs network I/O.

**Gatekeeper rule:** `driftc` is the final authority on whether a package is trusted for use.
Even if `drift` verified a package at download time, `driftc` must re-check trust at use time because
trust can change over time (e.g., key rotation, revocation lists, or namespace policy updates).

`driftc` is a verifier/consumer only: it never signs packages, never publishes packages, and never compresses artifacts for distribution.

**Implementation status:** package containers are hash-verified, and `driftc` verifies signature sidecars (when required by policy) before consuming any package contents.
Package signing itself is performed by `drift` as a separate, offline publishing step.

Pinned defaults (MVP):
- Trust store is project-local by default: `./drift/trust.json` (override with `--trust-store`).
- A user trust store may be optionally loaded (`~/.config/drift/trust.json`), and can be disabled for CI determinism (`--no-user-trust-store`).
- Unsigned packages are accepted only for local build outputs (e.g. `build/drift/localpkgs/`) unless explicitly allowed (`--allow-unsigned-from`).
- `--require-signatures` forces signatures everywhere, including local outputs.

### 2.2 `drift` (package manager / ecosystem tool)

`drift` is responsible for:
- Fetching packages from remote **repositories** (HTTP/S3/etc.)
- Managing a **project-local** dependency cache
- Optionally verifying signatures at download time (early rejection; `driftc` still verifies at use time)
- Resolving dependency graphs and versions
- Writing/updating the lockfile
- Producing vendor snapshots for CI/offline use
- Repository discovery configuration (`drift-sources.json`)
- Publisher tooling: building and signing repository indexes

---

## 3. Repository layout (typical)

```
repo/
  drift-project.json
  drift-project.lock.json

  packages/
    core/
      drift-target.json
      src/

    net/
      drift-target.json
      src/

  apps/
    cli/
      drift-target.json
      src/

  build/
    drift/

  cache/
    driftpm/

  vendor/
    driftpkgs/
```

Notes:
- `build/` contains build outputs only (safe to delete).
- `cache/` contains downloaded dependencies for this project (safe to delete; will re-download).
- `vendor/` is an optional snapshot for CI/offline/shipping scenarios.

---

## 4. Project file (`drift-project.json`)

The project file defines **policy and dependency universe**, not build units.

It includes:
- Project identity and version (**epoch label only**; not used for compatibility)
- Dependency catalog (keys → packages + constraints + repository)
- Repository definitions
- Trust policy
- Default target

Example:

```json
{
  "format": 1,
  "project": { "name": "example", "version": "1.0.0" },

  "repositories": {
    "toolchain": { "type": "toolchain" },
    "default": { "type": "http", "url": "https://packages.example.com/drift" }
  },

  "deps": {
    "std":  { "package": "std",       "version": "^0.3", "repository": "toolchain" },
    "http": { "package": "acme.http", "version": "^1.4", "repository": "default" }
  },

  "trust": {
    "require_signed": true
  },

  "default_target": "cli"
}
```

Terminology: this spec uses **repository** (not registry).

---

## 5. Target files (`drift-target.json`)

Each directory containing `drift-target.json` declares itself as a build target.

The filename is a **marker**, not an identifier.

### 5.1 Common fields
- `name` – unique target name
- `kind` – `"package"` or `"exec"`
- `version` – semantic version (packages only; used for compatibility)
- `uses` – dependency keys from project file (no URLs/versions here)
- `depends_on` – other local targets (typically packages)

### Package target example
```json
{
  "format": 1,
  "kind": "package",
  "name": "core",
  "version": "0.9.0",
  "modules": ["example.core"],
  "uses": ["std"]
}
```

### Executable target example
```json
{
  "format": 1,
  "kind": "exec",
  "name": "cli",
  "entry": "example.cli.main",
  "uses": ["std"],
  "depends_on": ["core"]
}
```

---

## 6. Target discovery

Targets are discovered by:
- Recursively scanning the project directory
- Any directory containing `drift-target.json` is a target

Excluded directories:
- `build/`
- `cache/`
- `vendor/`
- `.git/`

Target names must be unique within a project.

---

## 7. Dependency resolution model

### 7.1 External dependencies (downloaded)
- Defined in `drift-project.json` under `deps`
- Resolved by `drift`
- Downloaded into **project-local** cache:
  - `cache/driftpm/`

### 7.2 Internal dependencies (local packages)
- Expressed via `depends_on`
- Built automatically by `driftc` in dependency order
- Published locally into:
  - `build/drift/localpkgs/`

Internal packages are immediately available for downstream targets **in the same build invocation**.
This removes the Maven-style “publish before use” pain.

---

## 8. Package roots during build

Effective package root order during a build:

1. `build/drift/localpkgs/` (internal packages built from this repo)
2. `vendor/driftpkgs/` (if present)
3. `cache/driftpm/` (project-local downloaded dependency cache)
4. Explicit `--package-root` flags

No global cache is used unless explicitly configured (global caches are opt-in only).

Signature policy:
- Packages produced locally into `build/drift/localpkgs/` do not need to be signed (they are part of the local workspace build).
- Any package consumed from `vendor/` or `cache/` is expected to be signed for distribution and must pass `driftc` verification before use.

### 8.1 Package artifact format and signatures

- Package artifacts are stored as **DMIR-PKG v0** containers (`*.dmp`) produced and consumed by `driftc`.
- **Compression:** DMIR-PKG v0 containers are stored uncompressed. Compression, if used, is applied as an **outer transport layer** (Zstandard recommended) and is not part of the container format nor the signed payload.
- Published packages may include a **signature sidecar** file next to the container:
  - `pkg.dmp` (DMIR-PKG container bytes)
  - `pkg.dmp.sig` (JSON signature metadata)
- `drift` signs artifacts for distribution; `driftc` verifies signatures **at use time** (gatekeeper) using only local trust configuration.
- For the normative container and sidecar schema, see `docs/design/drift-lang-spec.md` (Chapter “DMIR-PKG v0”).

---

## 9. Build outputs

Default build output directory (relative to project root):

```
build/drift/
  targets/
    <target-name>/
      artifact/
      intermediates/
```

Override:
```bash
driftc --build-dir <path>
```

Output anchoring rule:
- By default, outputs are relative to the **project directory**, not the current working directory.
- `--build-dir` may be used to place outputs elsewhere (useful for CI packaging).

---

## 10. Invocation model

### Default invocation
```bash
driftc
```

Sugar for “build the default target” using the project file in the current directory.

### Explicit project or target
```bash
driftc --project=/path/to/drift-project.json --target cli
```

---

## 11. Lockfile (`drift-project.lock.json`)

Generated and updated by `drift`.

Includes:
- Exact package versions selected
- Artifact hashes
- Signer identity
- Monotonic lock revision (`lock_rev`) for “newer vs older”
- Generation timestamp

The lockfile is authoritative for reproducible builds.

---

## 12. Versioning rules

- **Compatibility and dependency resolution use only target/package SemVer.**
- Project version is an **epoch label only** (no role in compatibility).
- Dependency refresh should bump the project epoch and/or lock revision.
- Artifact filenames may include both for readability (informational only), e.g.:
  - `core-0.9.0-1.0.0-r42.dmp`

Metadata is authoritative; filenames are friendly tags.

---

## 13. Trust model defaults

### Reserved namespaces
Core namespaces are **reserved** and **must** be signed by toolchain-shipped keys:
- `std.*`
- `lang.*`
- `drift.*`

No TOFU is permitted for these namespaces.

### TOFU for third-party namespaces
For new third-party namespaces, default behavior is TOFU (trust-on-first-use):
- On first install of a package in a new namespace, `drift` prompts to trust the signer key for that namespace (e.g. `acme.*`).
- The accepted namespace→key binding is recorded locally (project-local by default).
- Subsequent signer changes for that namespace are rejected unless explicitly accepted (key rotation workflow).

### Trust store schema (v0, JSON)

Pinned (MVP):
- `drift` manages trust stores (UX / TOFU / key rotation).
- `driftc` is the offline gatekeeper and verifies trust at *use time*.
- Trust stores are project-local by default: `./drift/trust.json` (override with `--trust-store`).
- A user trust store may be optionally loaded: `~/.config/drift/trust.json` (disable with `--no-user-trust-store`).

Canonical trust-store format:

```json
{
  "format": "drift-trust",
  "version": 0,
  "namespaces": {
    "acme.*": ["ed25519:...kid..."]
  },
  "keys": {
    "ed25519:...kid...": { "algo": "ed25519", "pubkey": "<base64 32 bytes>" }
  },
  "revoked": {
    "ed25519:...kid...": { "reason": "...", "revoked_at": "..." }
  }
}
```

Notes:
- `kid` is derived from the public key bytes and is stable (see implementation docs in `lang2/driftc/packages/signature_v0.py`).
- `revoked` is canonical as a JSON object keyed by `kid`. For backward compatibility, `driftc` also accepts legacy `revoked: ["<kid>", ...]`.
- Namespace keys may be exact (`acme.crypto`) or prefix (`acme.*`), and `driftc` selects the most specific (longest) match.

### Signature sidecar schema (v0, JSON)

Pinned (MVP):
- The package container is `pkg.dmp` (DMIR-PKG v0, uncompressed).
- The signature sidecar is `pkg.dmp.sig` (JSON).
- A sidecar may contain **multiple signatures**.
- `driftc` verifies signatures **only** using trust-store keys (no TOFU); any `pubkey` embedded in the sidecar is informational only.

Canonical sidecar format:

```json
{
  "format": "dmir-pkg-sig",
  "version": 0,
  "package_sha256": "sha256:<hex>",
  "signatures": [
    { "algo": "ed25519", "kid": "ed25519:...kid...", "sig": "<base64 64 bytes>", "pubkey": "<optional base64 32 bytes>" }
  ]
}
```

What is signed:
- `package_sha256` is the sha256 of the raw `pkg.dmp` bytes.
- Each signature is computed over the raw `pkg.dmp` bytes (byte-for-byte).
  This includes package metadata stored in the DMIR-PKG manifest (for example
  `surfaces`, `entry_modules`, and `import_notice`), so those fields are
  authenticated. See the language spec for the normative schema.
- Compression (e.g., `.zst`) is an outer transport layer; it is not part of the container format nor the signed payload.

Precedence rules (pinned):
- Revocation wins: if a key id is revoked in the trust store, any signature by that key is ignored.
- Namespace allowlists win: a valid signature is accepted only if the signer `kid` is allowed for the module id(s) in the package.
- Multi-signature acceptance: **any** valid + allowed + not-revoked signature is sufficient to accept the package.

---

## 14. Discovery and repositories

### 14.1 Repository index: `drift-index.json` (per-repository)
A repository is represented by a signed, static **index snapshot** (`drift-index.json`) that lists:
- packages
- versions
- artifact URLs
- sha256 hashes
- signer key IDs
- optional tags/description

Packages themselves may be hosted anywhere; the index is metadata only.

### 14.2 Repository directory (index-of-indexes): `drift-sources.json`
Discovery at ecosystem scale uses **`drift-sources.json`**, a directory of repository indexes.
This is the canonical, toolchain-recognized name for the index-of-indexes.

### 14.3 First-run discovery behavior (no implicit centralization)
If the user runs a discovery command (e.g. `drift search <keywords>`) and no sources are configured:
- `drift` reports “no discovery sources configured”
- `drift` **suggests** an optional community `drift-sources.json` URL
- nothing is added unless the user opts in
- users may override sources to anything they want

This keeps discovery convenient without enforcing a central service.

---

## 15. Publisher tooling (indexes)

`drift` should support generating repository indexes so anyone can host their own repository.

Example flow:
- Build package artifacts (DMP/DMIR) in your release pipeline
- Place them in a directory intended for hosting (or mirroring)
- Generate an index snapshot from those artifacts
- Sign the index snapshot
- Publish the artifacts + index via any static hosting

Example commands (illustrative):
```bash
drift index build --artifacts ./public-artifacts --out ./drift-index.json --base-url https://downloads.example.com/drift/
drift index sign --in ./drift-index.json --key ./keys/index_ed25519.key
```

This supports community growth without central storage.

---

## 16. Determinism guarantees

A build is fully determined by:
- Source tree
- `drift-project.json`
- `drift-project.lock.json`
- Package roots (localpkgs/vendor/cache/explicit roots)
- Trust store and namespace policy

No network access occurs during compilation.

---

## 17. Summary

Drift’s tooling model:
- Keeps the compiler boring and predictable (`driftc`, offline)
- Keeps package acquisition and discovery in a separate tool (`drift`)
- Enables community publishing via signed artifacts + signed repository indexes
- Avoids Maven-style “publish before use” pain via project-local `localpkgs`
- Avoids mandatory centralization via opt-in `drift-sources.json` discovery


---

## 18. Platform selectors (pinned day-1)

Even though Drift IR is architecture-neutral, packages may:
- require specific **OS / ABI capabilities** (POSIX, epoll, etc.), and/or
- include **native components** that must be built per platform.

To avoid ambiguous distributions, Drift defines a **platform selector** used consistently across:
- lockfiles
- package cache keys
- repository index entries (when variants exist)

### 18.1 Platform descriptor

Canonical fields:
- `os` (string): `linux | macos | windows | freebsd | ...`
- `arch` (string): `x86_64 | aarch64 | riscv64 | ...`
- `abi` (string, optional): `sysv | msvc | gnu | ...`
- `libc` (string, optional): `glibc | musl | ...`
- `capabilities` (array of strings): `posix`, `epoll`, `kqueue`, `io_uring`, `threads`, `sockets`, `dlopen`, ...

Example:

```json
{
  "os": "linux",
  "arch": "x86_64",
  "abi": "sysv",
  "libc": "glibc",
	"capabilities": ["posix", "epoll", "threads", "sockets"]
}
```

### 18.2 Package platform requirements

Targets/packages may declare minimum requirements in `drift-target.json`:

```json
{
  "platform": {
    "os": ["linux"],
    "capabilities": ["posix", "epoll"],
    "libc": ["glibc", "musl"]
  }
}
```

---

## 19. Operational CLI contracts (current toolchain)

### 19.1 `drift fetch --json`
- Output is **JSON only** (no human text mixed in).
- Shape:
  - `ok: bool`
  - `mode: "lock" | "unlocked"`
  - `selected[]`: per package `{ package_id, identity, source_id, index_path, artifact_path, cache_path, sha256, signer_kids, unsigned }`
  - `errors[]`: structured errors with `reason_code`, identities, paths, sha values.
  - `cache_index_written: bool`
- Exit codes:
  - `0` on success
  - `2` on any error
- Lock mode: no resolution; requires matching sources and exact `lock.path` + sha; failures are fatal.

### 19.2 Cache layout invariant
- Cache packages are stored under `cache/driftpm/pkgs/<lock-normalized-path>`.
- `lock.path` is the normalized relative path (no `.`/`..`); the cache **must** follow this shape.
- Filename-only caches are invalid; if vendoring fails on a missing cache path, rerun `drift fetch` with the current toolchain to rebuild the cache in the normalized layout.

### 19.3 `drift doctor`
- Severities: `fatal`, `degraded`, `info`, `ok`.
- Exit codes (`--fail-on`):
  - `0`: no fatal; and when `--fail-on degraded`, also no degraded.
  - `1`: degraded findings when `--fail-on degraded`.
  - `2`: any fatal findings.
- Flags:
  - `--json` (machine output only), `--deep` (expensive existence/hash checks), `--cache-dir` (default `cache/driftpm`), `--vendor-dir` (default `vendor/driftpkgs`), `--fail-on fatal|degraded`.
- Checks (each has `check_id`, findings sorted, includes sha expected/got + paths on mismatches):
  - `sources` (schema + existence)
  - `indexes` (schema + optional deep file/hash/identity)
  - `trust` (schema + key/namespace sanity)
  - `lock` (schema + optional deep source bytes/signature checks)
  - `vendor_consistency` (deep only, degraded-only): missing vendor dir (`VENDOR_DIR_MISSING`), missing vendored artifact (`VENDOR_MISSING_ARTIFACT`), sha mismatch (`VENDOR_SHA_MISMATCH`)
  - `cache_consistency` (deep only, degraded-only): missing cache dir/index (`CACHE_MISSING`, `CACHE_INDEX_MISSING`/`CACHE_INDEX_INVALID`), missing cache entry (`LOCK_CACHE_MISSING_ENTRY`), sha divergence (`LOCK_CACHE_DIVERGENCE`), missing artifact (`CACHE_MISSING_ARTIFACT`)
- JSON reports are strict: stderr should be empty in success/structured error paths; findings include `artifact_path` and `sha256_expected/got` where relevant.

Rules:
- `drift` must refuse to install incompatible packages.
- `driftc` must refuse to build if an incompatible package is in the closure.

---

## 19. Native components (Mode A: rebuild on install)

### 19.1 Policy
Drift prefers **source-available native components**.

Mode A (“system-native”):
- packages ship **source + a constrained build recipe**
- `drift install` compiles native shims locally (e.g., via clang)
- opaque prebuilts are not required

### 19.2 Expressing native build inputs (in `drift-target.json`)

```json
{
  "native": {
    "tool": "clang",
    "sources": ["native/sqlite_shim.c"],
    "include_dirs": ["native/include"],
    "defines": ["DRIFT_SHIM=1"],
    "cflags": ["-O2", "-fPIC"],
    "links": { "system": ["sqlite3"], "frameworks": [] },
    "outputs": { "kind": "objects", "files": ["sqlite_shim.o"] }
  }
}
```

Constraints:
- `driftc` does not execute scripts.
- native compilation is performed by `drift` during install (and cached).
- `driftc` only consumes declared outputs as link inputs.

### 19.3 Cache keying
Native outputs are cached per:
- package id/version/hash
- **platform selector**
- native recipe hash

Recommended:
`cache/driftpm/native/<pkg-id>/<pkg-ver>/<platform-key>/<recipe-hash>/`

---

## 20. Lockfile platform pinning

`drift-project.lock.json` must record the platform descriptor used for resolution:

```json
{
  "lock_format": 1,
  "project_version": "1.0.0",
  "lock_rev": 42,
  "generated_at": "2025-12-17T20:12:00Z",
  "platform": {
    "os": "linux",
    "arch": "x86_64",
    "abi": "sysv",
    "libc": "glibc",
    "capabilities": ["posix", "epoll", "threads", "sockets"]
  }
}
```

Day-1 rule: lockfiles are generated for a specific platform descriptor (no ambiguity).

---

## 21. Repository index platform requirements (day-1)

Repository indexes (`drift-index.json`) may include `platform_requirements` per version to keep discovery/install non-ambiguous:

```json
{
  "id": "acme.net.epoll",
  "versions": [
    {
      "version": "1.2.0",
      "artifact_url": "https://downloads.acme.example.com/drift/acme.net.epoll-1.2.0.dmp",
      "sha256": "…",
      "published_at": "2025-01-08T14:01:00Z",
      "platform_requirements": { "os": ["linux"], "capabilities": ["posix", "epoll"] }
    }
  ]
}
```

Index metadata is advisory; the signed package manifest remains authoritative.


---

## 22. Native build scope (explicit non-goal)

Drift is **not** a general-purpose native build system.

It intentionally does **not** attempt to replace or emulate:
- CMake
- Autoconf / Automake
- Meson
- Jam
- Makefiles
- Arbitrary shell-based build pipelines

### 22.1 Supported native builds (narrow and explicit)

Drift supports **native shims only**, defined as:
- Small, leaf-level C/C++ source files
- Explicitly listed inputs
- Deterministic compiler invocations
- No generated configuration steps
- No recursive native dependency graphs

Typical use cases:
- Drift ↔ C ABI adapters
- Runtime stubs
- Thin wrappers around **system-provided libraries**

### 22.2 Unsupported native builds (by design)

The following are explicitly out of scope:
- Vendoring full third-party C libraries
- Running `./configure`, `cmake`, `meson`, or similar tools
- Executing arbitrary scripts as part of a build
- Native dependency resolution beyond system libraries

If a package requires a complex native library, that library is treated as a **system dependency**
and must be installed via the operating system or external tooling.

### 22.3 Rationale

This constraint is intentional and permanent:
- Keeps builds auditable and reproducible
- Avoids security risks of executing untrusted build scripts
- Prevents Drift from becoming a second-class native build orchestrator
- Keeps the mental model simple for users

Drift’s responsibility ends at **compiling explicitly declared native shims** and linking them.

---

## 23. Native components: threat model and trust boundary

Native components (C shims) may be required by some packages.

**Threat model and trust boundary**
- Drift signatures guarantee authenticity of:
  - Drift IR (DMIR)
  - package metadata
  - native shim *source* and build recipe
- Native shims are compiled locally; compiled objects are **not signed**
- Trust therefore extends to:
  - the local compiler toolchain
  - the operating system
  - system libraries linked at build time
- This is intentional and unavoidable for native interop.
- Drift does **not** attempt to secure or replace OS-level trust.
