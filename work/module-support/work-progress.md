# Module Support (MVP) — work tracker

This tracker defines the plan to implement **modules/imports** in the compiler and
test harness, aligned with:

- `docs/design/drift-lang-spec.md` (semantics, especially Section “Imports”)
- `docs/design/drift-lang-grammar.md` (syntax)
- `docs/design/drift-tooling-and-packages.md` (driver inputs: module roots, package roots, and build modes)
- `docs/design/spec-change-requests/module_merge_and_artifact_generation.md`
  (older but useful design notes about multi-file modules and artifacts)

It is intentionally **implementation-oriented**: it pins key decisions, lists
milestones, and tracks what is done vs. still pending.

---

## Scope (Release‑1 MVP)

Goal: compile and run programs split across multiple `.drift` files and multiple
modules, with `import` enabling cross-module name resolution.

Build targets vs compilation units (pinned for clarity):

- End users generally think in terms of producing either:
  - an executable (compile+link a set of modules into one image), or
  - a package artifact (bundle one or more compiled modules with metadata).
- “Compile module” is an internal compilation unit step used by both targets
  (merge files, validate directives, build export interface, lower to IR); it is
  not a primary user-facing target in MVP.

Non-goals for MVP (explicitly deferred):

- Real package manager / registry / lockfiles.
- Publishing/distribution workflows for signed packages (handled by `drift`, not `driftc`).
- Rich visibility lattice / fine-grained privacy controls (e.g. `pub(crate)`, per-item `private/public/protected`, re-export graphs). MVP still requires an explicit export set for cross-module imports (Milestone 3).
- Interfaces/traits-based module boundaries and dynamic linking.

---

## Pinned decisions (to avoid churn)

### Canonical module id

- Source declares its module using:
  - `module <module_path>` (per grammar/spec).
- Canonical format is dotted lowercase segments, matching the spec:
  - `<module_name>.<submodule...>`
  - lowercase alnum segments separated by dots, with optional underscores inside segments
  - no leading/trailing/consecutive dots
  - no leading/trailing/consecutive underscores within a segment
  - total length ≤ 254 UTF-8 bytes
  - reserved prefixes are rejected: `lang.`, `abi.`, `std.`, `core.`, `lib.`

### Compilation unit + module membership

- The compiler **accepts multiple source files per module**.
- Module membership is determined by the `module ...` declaration in each file.
- The driver groups files by module id and compiles per-module as a unit.

Default build mode (MVP driver behavior):

- “Directory = module” convenience mode is driven by explicit module roots:
  - The driver is invoked with one or more module roots via:
    - `--module-path <path>` (repeatable)
    - `-M <path>` (repeatable)
  - Module roots are **explicitly required**; there is no implicit default root.
    Rationale (pinned): it keeps builds reproducible and avoids “ambient workspace”
    discovery rules that differ across tools/IDEs.
    Recommended invocation patterns:
    - Single-project build: `driftc -M . <entrypoint.drift>`
    - Workspace build: `driftc -M <workspace_root> <entrypoint.drift>` (with module ids inferred from directories under the root)
    - Add stdlib/vendor roots explicitly as needed: `driftc -M . -M <vendor_root> ...`
  - Module ids are inferred deterministically from directory paths under these roots
    (optionally under a fixed per-root prefix; exact CLI spelling pinned by the driver).
  - Every `.drift` file under a module directory must declare `module <id>`
    matching the inferred module id.
  - Missing `module` declarations or mismatches are compile-time errors (matches the spec’s single-module build constraint).

Language semantics remain directory-agnostic:
- The language defines module identity by `module <id>` and `import <id>`.
- Directory inference is a tooling convention, not a semantic requirement.

### Import MVP shape

- MVP implements **two import forms** (spec-aligned):
  - `import my.module [as alias]` binds a module name (namespace import).
  - `from my.module import Symbol [as alias]` binds an exported top-level symbol.

Provider-agnostic resolution (pinned):
- `import`/`from ... import ...` semantics are the same regardless of whether the imported module is provided as source or as a verified DMIR/DMP package.
- The driver resolves an imported module id against the set of available module providers (source roots and package roots).

### Resolution model (MVP)

- Names are resolved in three tiers:
  1) Local scope (locals/params)
  2) Current module top-level declarations
  3) Imported modules’ exported top-level declarations

MVP qualified module access (implemented):
- `import my.module as x` introduces a **module binding** `x`.
- `x.foo(...)` calls exported functions from the imported module.
- `x.Type` is permitted in type positions (e.g., `val p: x.Point = ...`) and
  module-qualified struct constructor calls are supported (`x.Point(...)`).
  This is resolved at compile time; there is no runtime “module object”.

### Internal symbol identity

- Every top-level symbol has an internal “module-qualified name”:
  - `(module_id, name)` for lookup, diagnostics, and codegen symbol naming.
- This is required to avoid collisions once multiple modules are compiled
  together.

### Prelude (MVP)

- `lang.core` is treated as an **implicit prelude module** (already in spec).
- Implementation strategy for MVP:
  - prelude is injected by the compiler/driver (not a source module yet).

### Multi-file module merge rules (MVP)

- It is an error if two files in the same module define:
  - the same function name
  - the same struct name
  - the same exception name
  - the same variant name
  - the same `implement <Type>` method signature (target + method name)
- Executable build: exactly one `main` across the whole program (same as today),
  but now checked across all input modules.

### Artifacts (deferred but planned)

- The design in `module_merge_and_artifact_generation.md` (per-file `.do` +
  link/merge step) is useful, but MVP starts with **in-memory merge** in the
  driver to keep scope contained.

---

## Milestones

### Milestone 1 — Multi-file compilation within a module

**Goal:** compile/run a module composed of multiple `.drift` files.

Work:
- Driver accepts multiple files and groups by `module` declaration.
- Merge top-level declarations across files (module-local namespace).
- Cross-file collision diagnostics:
  - duplicate function/type/exception/variant names
  - duplicate method definitions
- Ensure exception event FQNs use the declared module id (already pinned).

Tests:
- E2E: module split across files (e.g. `lib.drift` defines a function, `main.drift` calls it).
- E2E: duplicate function across files in same module → compile error (diagnostic pins both spans).

Status: completed

Progress note:
- Multi-file parsing/merge within a single module is implemented in `lang2/driftc/parser/__init__.py` via `parse_drift_files_to_hir(...)`.
- `driftc` CLI accepts multiple source paths and the e2e runner compiles all `*.drift` files in a case directory.
- E2E coverage:
  - `lang2/codegen/tests/e2e/module_multifile_basic` (lib+main split)
  - `lang2/codegen/tests/e2e/module_multifile_duplicate_fn` (cross-file duplicate diagnostic)

### Milestone 2 — Module imports and cross-module resolution

**Goal:** `import other.module` enables use of symbols from another module.

Work:
- Build a module graph from:
  - each file’s `import` list
  - the set of available modules (from input file set)
- Compile modules in dependency order (detect and reject cycles in MVP).
- Resolve `import` bindings:
  - module binding name = last segment or `as` alias
- Name resolution uses imported module bindings for:
  - function calls (`mod.fn(...)`)
  - type names (`mod.Type(...)` once qualified access exists)
  - exception constructors (`mod:Err()` already uses event fqn for catch; ctor naming must align)

Tests:
- E2E: two-module program (`module a`, `module b`, b imports a and calls `a.f()`).
- E2E: missing module import target → compile error with span.
- E2E: cyclic module imports → compile error (deterministic message).

Status: completed (MVP `from ... import ...` + module-qualified access)

Progress note (partial implementation):
- A workspace parser is available via `parse_drift_workspace_to_hir(...)`:
  - accepts an arbitrary set of `.drift` files,
  - groups by declared `module` id (defaulting to `main`),
  - merges each module using the existing Milestone 1 rules,
  - enforces explicit exports for `from <module> import <symbol>` imports,
  - builds a module dependency graph and rejects import cycles (parser-phase, pinned spans).
- MVP import binding rules implemented (module-scoped in MVP for simplicity):
  - importing the same module/symbol multiple times is idempotent (no-op),
  - alias/binding conflicts are diagnosed as errors,
  - missing module and non-exported symbol are diagnosed as errors.
- Function symbols are qualified as `<module>::<name>` internally to avoid collisions:
  - `main` remains the unqualified entry symbol (exactly one across the workspace).
  - call sites are rewritten so `from lib import add; add(...)` calls `lib::add`.
- Method symbols are also module-qualified to avoid collisions across modules:
  - `Type::method` becomes `<module>::Type::method` in the merged workspace unit.
- Stage2 lowering is hardened: method calls are always resolved and lowered as plain `Call` instructions.
  - There is no MIR `MethodCall` instruction in v1; any unresolved method call reaching MIR lowering is a hard assertion (checker/resolution bug).
- Module-qualified access is supported for both value and type namespaces:
  - `import lib as x; x.make(...)` resolves `make` in the imported module’s value exports.
  - `val p: x.Point = ...` resolves `Point` in the imported module’s type exports.
- E2E harness uses the workspace loader for all cases (single-file and multi-file) so missing-module imports are diagnosed consistently.
- E2E cases now unskipped and passing:
  - `import_basic_one_symbol`
  - `import_module_alias_call`
  - `import_multiple_files_same_module`
  - `import_alias`
  - `import_missing_module`
  - `import_missing_symbol`
  - `import_private_symbol` (explicit exports enforced; private-by-default)
  - `import_from_alias_conflict_across_files_ok` (per-file `from` bindings; no #pragma-once semantics)
  - `import_uses_type_and_value_namespaces` (separate namespaces for value vs type imports)
  - `import_chain`
  - `cycle_direct`, `cycle_indirect_3way`
  - `module_root_mismatch`, `same_basename_different_dirs`
  - Existing Milestone 1 cases remain green (`module_multifile_basic`, `module_multifile_duplicate_fn`).

Remaining work for Milestone 2:
- (none for MVP import resolution; remaining items are Milestone-4/package related)
- (Moved to Milestone 4 and now implemented): exported ABI-boundary enforcement for cross-module calls (always uses the public entrypoint wrapper).
- (Implemented): module-scoped nominal type identity (struct/variant/exception) so two modules can define `Point` without collisions.
- Keep one canonical “module namespace” design (future): extend `x.foo` to permit deeper module paths or richer import mechanisms without introducing runtime module objects.

Test maintenance (pinned):
- The repo contains a set of module/import-related e2e cases that are committed as `skip: true`
  placeholders (to avoid forgetting coverage while imports/exports are still landing).
- As each import/export milestone is implemented, review the skipped cases and unskip any
  that are now supported, keeping the e2e suite authoritative without duplicating coverage.

Skipped test inventory (non-exhaustive; keep up to date as new coverage is added):
- Deferred features: full re-export semantics (`pub use` / re-export graphs) and richer import UX beyond MVP.

### Milestone 3 — Exports / visibility (minimal, spec-aligned)

Pinned (MVP, required by the spec):

- The compiler maintains an internal **export set** per module.
- Items are **private by default**; only explicitly exported items are importable.
- Importing a non-exported symbol is a compile-time error.
- Exported functions form the module interface and are ABI-boundary entry points:
  they must use the cross-module can-throw ABI calling convention.

Surface syntax (MVP): still TBD, but must exist. Acceptable minimal forms:
Pinned (MVP): `export { foo, Bar, Baz }` at module top-level.

- The export list contains names of top-level items in the current module
  (functions and type-level symbols such as `struct`/`variant`/`exception`).
- The compiler forms the module’s export set from the union of all `export { ... }`
  declarations across files in the module.

Tests:
- Importing a private symbol should be a compile error.
- Exported functions must be compiled with the cross-module ABI shape.

Status: completed (explicit exports enforced; ABI-boundary enforcement deferred to package artifacts)

Progress note:
- Parser support for `export { ... }` and `from <module> import <symbol> [as alias]`
  is implemented in `lang2/driftc/parser/grammar.lark`, `lang2/driftc/parser/ast.py`,
  and `lang2/driftc/parser/parser.py`.
- Export sets are computed per module (union across files) and enforced by the
  workspace loader; imports of non-exported symbols are rejected (e2e:
  `import_private_symbol`).

Implementation note (pinned; avoid ABI leaks):
- The compiler must mark exported functions early (during module graph build/merge)
  with an explicit flag like `is_exported_entrypoint`.
- Downstream phases (checker/stage4/codegen) must treat this as authoritative:
  - package/module boundaries must not silently coerce between calling conventions
    (value return vs internal `Result<T, Error>` carrier)
  - the export set and per-export metadata must be recorded in package artifacts
    (DMIR/DMP) so that cross-package linking can enforce ABI compatibility.

Status update (MVP reality):
- Explicit exports are enforced and imports reject non-exported symbols.
- ABI-boundary calling convention enforcement is deferred until package artifact
  generation (Milestone 4), because source-workspace builds currently compile and
  link modules in one unit and do not yet emit boundary wrappers/metadata.

### Milestone 4 — Artifact generation (post-MVP, but planned now)

Aligns with `docs/design/spec-change-requests/module_merge_and_artifact_generation.md`.

Pinned clarifications (to avoid churn before implementation):

- CLI naming:
  - `-M/--module-path <dir>`: source module roots (repeatable)
  - `--package-root <dir>`: package roots (repeatable)
  - Avoid introducing `--package-path` unless it truly means a separate “search list”
    concept; repeatable roots + pinned ordering is clearer for MVP.

- Signing model (pinned: Option A):
  - `driftc` builds **unsigned** DMIR/DMP artifacts (deterministic manifest + blobs).
  - `drift` signs artifacts and handles publishing/index signing workflows.
  - `driftc` verifies signatures **at use time** when loading packages from local roots.
    Rationale (pinned):
    - Packages can appear via vendoring / local roots without `drift`.
    - `driftc` is the security boundary that consumes IR and must enforce trust policy.
    - Verification is offline and does not violate the “no network I/O” rule.

- Provider model:
  - Imports resolve by module id regardless of origin (source roots vs package roots).
  - `driftc` resolves modules against a set of ModuleProviders:
    - source provider(s) backed by `-M/--module-path`,
    - package provider(s) backed by the effective package roots.
  - Effective package root order during build (from tooling spec):
    1) `build/drift/localpkgs/`
    2) `vendor/driftpkgs/`
    3) `cache/driftpm/`
    4) explicit `--package-root` flags
    (No global cache unless explicitly configured.)

- Deterministic hashing:
  - Define canonical bytes for the manifest (stable field ordering, stable list ordering,
    UTF-8, no elision rules).
  - Blobs are content-addressed (e.g., sha256 over raw bytes) and referenced from the
    manifest.
  - Signature (added by `drift`) must cover the canonical manifest bytes + referenced
    blob hashes so verification is deterministic across platforms.

- Minimum interface table fields per exported symbol (for ABI enforcement):
  - `(module_id, symbol_name)`
  - fully resolved signature (types)
  - calling convention / boundary flags (e.g., can-throw boundary ABI)
  - exported-entrypoint bit

Work:
- Define the unsigned package artifact format (container, manifest, blobs) and how it
  embeds multiple modules per package.
- Implement package emission in `driftc` (unsigned only):
  - compile one or more modules, write deterministic manifest + IR blobs.
- Implement package verification and module loading in `driftc`:
  - load from local package roots only
  - verify signature + policy before using any package contents
- Add a link/merge step that:
  - merges symbol tables and rejects collisions
  - remaps TypeIds into a link-time unified table
  - enforces exported ABI boundary rules using the recorded interface table
  - produces either an executable or another unsigned package artifact

Status: in progress (MVP container + package consumption + TypeId remapping implemented; ABI-boundary enforcement deferred)

Progress note (current reality):
- Unsigned package artifacts are implemented (DMIR-PKG v0, deterministic manifest+blobs, hash-verified).
- `driftc` can:
  - emit unsigned packages (`--emit-package`) and load them from `--package-root`,
  - reject duplicate module ids across packages,
  - import package type tables into a unified link-time `TypeTable` and remap all package `TypeId`s into host `TypeId`s (link-time compatibility by merge/remap, not “equal fingerprints”),
  - include variant schemas in package payloads and link them into the host type system (needed for `Optional<T>` and future variants),
  - embed only the call-graph closure of referenced package functions into codegen.
- Signature sidecar verification is implemented (offline, at use time):
  - published packages may include `pkg.dmp.sig` sidecars (produced by `drift`),
  - `driftc` verifies signatures at use time (gatekeeper) using project/user trust stores,
  - unsigned packages are accepted only from local build outputs (`build/drift/localpkgs/`) or explicitly allowed unsigned roots.
- Deterministic type linking is implemented:
  - package type tables are merged via canonical type keys (order-independent),
  - builds are deterministic across package discovery/root ordering (locked by driver tests).
- Exported ABI-boundary enforcement for cross-module calls is implemented:
  - cross-module calls always target the public wrapper symbol and use the uniform `FnResult<Ok, Error*>` boundary ABI,
  - nothrow callers unwrap via a trap helper (never “blindly extract ok”),
  - locked by driver IR assertions and e2e tests (including by-value struct Ok payload).
- Minimal trust UX tooling exists (`lang2/drift`):
  - `drift sign pkg.dmp --key key.seed` emits `pkg.dmp.sig`,
  - `drift trust add-key/list/revoke` manages a project-local `./drift/trust.json`,
  - revocation is enforced by `driftc` at use time (locked by driver test: accept → revoke → reject).

Remaining work for Milestone 4 (pinned for later):
- Tighten and document the package interface table schema further as the package format evolves.
- Implement `drift` package-manager/publisher flows (fetch/cache/vendor/publish), key UX, and repository/index signing (out of scope for `driftc`).

---

## Next steps (pinned; `drift` tool + packages)

These are the pinned phases for the next major step: making the “publish → depend → revoke” loop real across projects.

Pinned goals:
- Single version per package id per build (hard error if multiple versions of the same `package_id` are present in the build graph).
- Support exported `exception` / `variant` / `const` in module interfaces (packages).
- Forbid exported methods (MVP guardrail; methods remain module-internal).

### Phase 1 — Repository workflow in `drift`: publish → fetch → vendor

Goal: another project can depend on packages without manual copying.

1. `drift publish --dest-dir <dir> <pkg.dmp>...`
- Validate inputs exist and sidecars exist (or allow unsigned only with an explicit flag).
- Copy `pkg.dmp` + `pkg.dmp.sig` into dest.
- Update a simple `index.json` in dest:
  - key: `{package_id, version, target}`
  - values: paths + sha256 + signers (kids)
- Enforce “single version per package id” at publish time (error if dest already has a different version of the same `package_id` unless `--force`).

2. `drift fetch --sources drift-sources.json`
- Read repo indexes listed in `drift-sources.json`.
- Download/copy packages into project-local cache (pinned cache layout in tooling spec).
- Verify signature + trust policy at fetch time (early rejection), but `driftc` remains the final gatekeeper.

3. `drift vendor`
- Copy exact resolved artifacts into `vendor/driftpkgs/` for CI/offline.
- Write a lock file (minimal `drift.lock.json`) mapping `package_id` → exact version + hashes.

Status: completed (dir-only MVP)

Progress note:
- Implemented `drift publish` (directory repository with `index.json`, one version per `package_id` unless `--force`).
- Implemented `drift fetch` (MVP: directory sources only, offline copy into `./cache/driftpm` with merged `index.json`).
- Implemented `drift vendor` (copies from cache into `./vendor/driftpkgs` and writes `drift.lock.json`).
- Locked by driver test `lang2/tests/driver/tests/test_drift_publish_fetch_vendor.py`.

### Phase 2 — Package identity/version fields

Goal: drift can resolve; driftc can validate.

4. Add to the package manifest (and/or interface header):
- `package_id` (namespaced)
- `package_version` (SemVer)
- `target` (triple)
- `build_epoch` optional tag (non-semantic)

5. `driftc` load-time validation:
- Reject missing identity fields.
- Reject multiple packages with the same `package_id` but different versions in the build graph.

Status: completed

Progress note:
- `driftc --emit-package` requires `--package-id`, `--package-version`, and `--package-target` and writes them into the manifest.
- `driftc` rejects any loaded package missing identity fields (package-phase diagnostic).
- `driftc` enforces “single version per package id per build” (hard error when the build graph contains conflicting versions/targets or different artifacts for the same id).

### Phase 3 — Interface completeness: exported exceptions/variants/consts

Goal: module interface is authoritative and sufficient for import/typecheck/codegen.

6. Extend `drift-module-interface v0` to include:
- `exports.values` / `exports.types` / `exports.consts`
- `signatures` for exported values (already)
- `exception_schemas`: exported exception name → ordered fields `{name, type}`
- `variant_schemas`: exported variant base name → type params + arms + field schemas
- `consts`: exported const name → `{type, value_encoding}` (pinned deterministic encoding)

7. Tighten provider validation (load time):
- Interface schemas must exactly match payload schemas for exported items.
- Interface must not list non-exported entries (already enforced for signatures; extend to schemas).
- Hard reject exported methods in the interface.

8. Add driver negative tests (one per rule):
- exported exception missing schema
- exported variant schema mismatch
- exported const missing type/value
- method listed in exports → reject

### Phase 4 — Re-export authority

Goal: `export { foo }` where `foo` is imported becomes fully supported and reflected.

9. Make re-exports explicit and validated:
- Keep the trampoline strategy: `a::foo` exists as a real exported wrapper in the payload and appears normally in the interface.
- Add tests:
  - re-exported function callable from a third module
  - re-exported type usable from a third module
  - ambiguity errors when two imports try to re-export the same local name

Concrete next commit (pinned):
- Implement Phase 1 (publish/fetch/vendor) next; Phase 2 (identity/version fields) is complete.

---

## Implementation notes / known hard parts

### Namespacing and `TypeId`

Current compiler data structures often treat `TypeId` names as globally unique.
To support modules properly long-term, we must ensure:

- Types are keyed by `(module_id, name)` internally.
- Diagnostics report the correct module-qualified names.
- Codegen emits stable symbol names that include module id to avoid linker collisions.

### Imports vs syntax for qualified access

If the language only has member access (`.` / `->`) for value expressions, we
must decide how to represent module-qualified symbol access in the AST/HIR:

- Either treat `mod.fn` as a special “qualified name” expression (not a field).
- Or treat module bindings as values with a restricted “namespace object” type.

This is a major clarity choice; MVP should pick the simplest representation that
does not leak into runtime semantics.

---

## Status summary

- Milestone 1: completed
- Milestone 2: completed (workspace loader + `from <module> import <symbol> [as alias]`, cycle detection, idempotent per-file imports)
- Milestone 3: completed for explicit exports + visibility enforcement (explicit export set required; importing non-exported symbols is an error).
- Milestone 4: in progress (package artifacts + trust pipeline); MVP DMIR-PKG, type linking, and ABI-boundary call enforcement are implemented; remaining work is Phase 1 drift workflows + Phase 3 interface completeness + Phase 4 re-export authority.
