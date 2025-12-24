# Method impl matching across modules (workspace)

## Status (visibility detour)
- Spec now uses **pub + export list** for visibility and **module-only imports**.
- `export { module.* }` re-exports are the only cross-module surface mechanism.
- This milestone assumes those rules when defining “visible modules” and “public methods”.
- Workspace impl matching now works across modules after:
  - preserving impl type params during module merge,
  - predeclaring struct schemas before per-module lowering (prevents empty-field instantiations),
  - and including external modules in visibility deps.
- `lang2-driver-suite` passes after updating method-resolution tests to use qualified impl targets.

## Goal
Enable method/impl matching across module boundaries using a workspace-wide impl index and call-site visibility, with deterministic ambiguity handling and production-grade diagnostics.

## Non-goals (for this milestone)
- Full trait solving / where-clause enforcement (only the plumbing needed for lookup).
- Specialization / “most specific impl wins”.
- Cross-package inherent impls for foreign types (defer; see coherence).

## Production requirements to lock in now
- **Workspace-wide impl index**: method resolution must not depend on “current module only” scans.
- **Canonical type identity** across modules (`TypeId` must be globally stable within the compilation unit).
- **Visibility correctness**: candidates are filtered by call-site visibility and method/impl visibility.
- **Deterministic resolution**: stable ordering + explicit ambiguity errors; no “first one wins”.
- **Coherence checks** at link time: catch duplicate/conflicting method definitions early with actionable errors.

## Phase M0 — Rules and invariants
- **Inherent impl orphan rule (recommended)**: an inherent `implement <Type> { ... }` is allowed only if the receiver type is defined in the same **package** (or same compilation unit, depending on your packaging model). This permits cross-module methods inside a package while preventing “impl foreign type” chaos across packages.
- **Multiple impl blocks** are allowed, but enforce **no duplicate method signatures** for the same receiver type (after generics are instantiated, this becomes “no overlapping signatures”; for now, exact duplicates).
- Define method lookup order:
  1. inherent methods
  2. trait methods only if/when trait methods are allowed via dot-call and the trait is in scope (UFCS can be added later)

## Phase M1 — Driver exports a workspace view
Driver must emit per module (in HIR export metadata):
- exported types (type defs + canonical `TypeId`)
- exported callable decls (free fns, ctors/qualified members if applicable)
- impl blocks:
  - impl header: `impl_id`, `def_module`, `visibility`, `type_params` (even if empty), `target_type`, optional `trait_ref`, optional `where_clause`
  - methods: name, full signature (including receiver kind), visibility, span

Implementation detail:
- Build a **workspace module graph** and compute, per module, the set of **visible modules** from:
  - current module
  - its imports
  - re-exports (if supported)
  - (do not require tests to inject `visible_modules` manually; tests should build imports that drive visibility)

## Phase M2 — Build the global impl index (link step)
After all modules are lowered and type identities are canonical:
- Build two indexes (even if traits are not fully implemented yet):
  - **Inherent index**: `(canonical_target_type, method_name) -> [method_candidate]`
  - **Trait index** (stub OK): `(trait_id, canonical_target_type) -> [impl]`, and `(trait_id, method_name) -> [decl]`
- Store enough provenance on each candidate for diagnostics:
  - defining module
  - impl header span
  - method span

Add a link-time validation pass:
- enforce the inherent orphan rule
- detect duplicate inherent methods (same receiver type + same method name + same erased signature) across visible impls in the same package
- emit errors that point to both definitions

## Phase M3 — Method call resolution uses the workspace index
Method lookup flow for `recv.m(args...)`:
1. Resolve receiver expression type to a canonical `TypeId` (or best-known shape; generics can be placeholders).
2. Query inherent index by `(recv_base_type, "m")`.
3. Filter candidates by visibility relative to the call-site module:
   - candidate is usable if `pub`, or defined in the same module, or (later) via explicit re-export rules.
   - also apply method-level visibility, not just impl-level.
4. Apply impl-template matching/unification (existing unification):
   - match receiver type against impl target type
   - compute substitutions
5. Instantiate candidate method signature under substitution.
6. Run overload resolution among remaining candidates.
7. If exactly one best candidate: select it.
8. If none: “no method named … for type …” and include near-misses (same name but not visible; same name but receiver mismatch) when available.
9. If multiple tie for best: ambiguity error listing all tied candidates and their defining modules.

Determinism:
- Resolution must be independent of module iteration order. Enforce a stable sort key for candidates, e.g. `(def_module_id, impl_id, method_id)` before scoring, and tie-break only by explicit rules (never by discovery order).

## Phase M4 — Diagnostics quality bar
Ambiguity:
- show each candidate as: `module::Type.method(sig)` with source spans
- state why ambiguous (equal match score / same conversion cost)

Not visible:
- if a matching method exists but is not visible, emit: “method exists but is not visible here” and point to the definition span

Coherence violation:
- duplicate method signature detected at link time: point to both definitions and recommend disambiguation (rename or remove one, or stop importing both modules if that is a supported fix).

## Tests (high signal)
1. Cross-module success (basic)
   - `m_box`: `struct Box<T>`, `implement<T> Box<T> { pub fn tag(self) ... }`
   - `m_main`: `import m_box; Box<Int>{...}.tag()` resolves

2. Impl in different module than type (same package)
   - `m_types`: `struct S`
   - `m_impls`: `import m_types; implement S { pub fn m(self) ... }`
   - `m_main`: imports `m_types` and `m_impls`; `S{}.m()` resolves

3. Import controls visibility
   - `m_a`: `implement S { pub fn m(self) ... }`
   - `m_b`: `implement S { pub fn m(self) ... }`
   - `m_main`: imports only `m_a` → resolves
   - `m_main2`: imports `m_a` and `m_b` → ambiguity error citing both modules

4. Private method blocked
   - `m_a`: `implement S { fn hidden(self) ... }`
   - `m_main`: imports `m_a`; `S{}.hidden()` errors as “exists but not visible”

5. Generic impl across modules (template matching)
   - `m_a`: `implement<T> Box<Array<T>> { pub fn inner(self) returns T }`
   - `m_main`: `Box<Array<Int>>.inner()` typechecks as `Int`

## Exit criteria
- Multi-module method calls resolve using the global workspace index (no per-module scans at call sites).
- Visibility rules are enforced for methods and impls.
- Ambiguity and coherence diagnostics cite contributing modules and point at source spans.
- Existing single-module method/impl tests pass unchanged.

## After this
- Add trait method lookup (dot-call via in-scope traits) + where-clause enforcement on the same impl index.
- Then proceed to borrow/lifetime upgrades (NLL-lite/place-model expansion).
