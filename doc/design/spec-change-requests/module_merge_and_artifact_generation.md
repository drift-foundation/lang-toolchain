# Drift Module Merge and Artifact Generation

## Motivation

We want to compile multiple Drift source files without forcing a single file per module. The compiler should:

- Allow multiple files to contribute to the same module.
- Detect cross-file collisions (types/functions/methods/interfaces).
- Enforce a single `main` when producing an executable.
- Support producing a reusable “Drift Object” artifact for later linking/packaging.

## Proposed Flow

### Per-file compilation (`driftc --object`)

Input: one `.drift` file.

Pipeline:

1) Parse → HIR → type check → borrow check (mandatory).
2) Lower to MIR (optionally SSA) as the persisted code stage.
3) Emit a “Drift Object” (`.do`) artifact containing:
   - Module name (from the source) and a stable module id (per driver).
   - Public symbol table:
     - Free functions: display name, module id, signature (param/ret `TypeId`s), callable_id.
     - Methods: method name, `impl_target_type_id`, `self_mode`, signature, callable_id.
     - Types/interfaces exported (for future collision checks).
   - Visibility per symbol (public/private).
   - Stage payload:
     - Normalized HIR or MIR for each symbol (optional SSA/throw summaries if needed).
   - TypeTable snapshot or a relocatable type index so `TypeId`s can be remapped at link time.
   - Diagnostics (if any) to fail early.

### Link/merge phase (`driftc --link`)

Inputs: one or more `.do` files.

Responsibilities:

- Assign stable module IDs across files (respecting module names).
- Merge symbol tables and reject collisions:
  - Free vs free with same `(module, name)`.
  - Method duplicates per `(impl_target_type_id, method name)`.
  - Free vs method name collisions within a module (per spec).
  - Public type/interface collisions.
- Enforce exactly one `main` when producing an executable; allow none for a library/package.
- Reconcile `TypeTable`/`TypeId`s:
  - Unify common builtins.
  - Remap per-file `TypeId`s into a link-time table.
- Build a unified `CallableRegistry` (module-aware) for any post-link resolution checks.
- Optionally rerun cross-file invariants (type/borrow) if needed.

Outputs:

- For a package: merged metadata and (optionally) codegen-ready IR.
- For an executable: linked/codegen’d artifact from merged MIR/SSA.

### CLI sketch

- Per-file object: `driftc --object file.drift -o file.do`
- Link/merge: `driftc --link a.do b.do ... -o app` (executable) or `-o lib.do` (package)

## Rationale

- Per-file compilation is fast and local; no need to load all sources at once.
- The link phase is the single place to enforce cross-file correctness and visibility.
- FnN resolution and borrow checking remain module-aware once `TypeId`s and `module_id`s are unified.
