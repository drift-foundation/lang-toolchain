# Source Semantic Ingestion / Ordering Refactor

## Status: proposal (review before execution)

---

## 1. Current State

### What is already phase-separated cleanly

The source compilation pipeline in `parse_drift_workspace_to_hir`
(`parser/__init__.py:1452-3976`) has several phases that are genuinely
order-independent:

1. **AST parsing** (lines 1576-1603): all source files are parsed before any
   semantic work begins. File order is deterministic (sorted by content hash).

2. **Module grouping and validation** (lines 1633-1770): modules are organized
   by ID, one-file-per-module constraint is enforced. No type interactions.

3. **Export interface resolution** (lines 2136-2387): all modules' `pub`
   declarations and `export {}` blocks are scanned. Star re-exports are resolved
   iteratively until convergence. This is a genuine surface-symbol pre-pass
   operating on names only, not TypeIds.

4. **Import validation and dependency graph** (lines 2389-2634): imports are
   checked against export interfaces. Module dependency edges are computed.
   Trait scopes (`use trait`) are resolved. All symbol-level, no type identity.

5. **Module-qualified type-reference rewriting** (lines 2639-2918):
   `module_alias.TypeName` references in type expressions are rewritten to
   canonical `(module_id, name)` pairs using the import/alias tables. This is
   name-level only.

6. **Package ingress** (handled by `driftc.py` before `parse_drift_workspace_to_hir`
   is called): all package types are linked into the shared TypeTable via
   `import_type_tables_and_build_typeid_maps` before any source parsing. Package
   type identity is fully resolved, deterministic, and order-independent (Phase
   A/B canonical key system in `type_table_link_v0.py`).

7. **Pub type alias pre-registration** (lines 3233-3253, added in 0.27.131):
   all public type aliases from all modules are registered in the shared
   TypeTable before any module is lowered. This was the fix for the
   FORWARD_NOMINAL divergence in drift-web `add_route`.

8. **Post-lowering FORWARD_NOMINAL coercion** (lines 3916-3963): a sweep over
   all signatures that resolves remaining FORWARD_NOMINALs to concrete types
   using the now-complete TypeTable.

9. **Checker invocation** (`driftc.py:3160+`): type checking starts only after
   all lowering, signature resolution, and `SemanticWorld` assembly is complete.
   `SemanticWorld.assert_ready()` enforces this. No interleaving with lowering.

### What is still order-sensitive

The critical gap is **Phase 2** (lines 3255-3301): per-module lowering. Each
module is lowered sequentially via `_lower_parsed_program_to_hir`, and inside
that function the following happens against the shared TypeTable:

| Registration step | What it does | Cross-module visible? |
|---|---|---|
| Exception schema registration (line 4036-4047) | Adds `module:ExcName` to `exception_schemas` | Yes — all modules share the table |
| Struct declaration (lines 4203-4221) | `declare_struct` with placeholder fields | Yes — `get_struct_base` / `get_nominal` |
| Interface declaration (lines 4222-4278) | `declare_interface`, method schemas, parents | Yes — `get_interface_base` / `require_nominal` |
| Variant declaration (lines 4279-4328) | `declare_variant` with arm schemas | Yes — `get_variant_base` |
| Struct field TypeId fill (lines 4329-4349) | `resolve_opaque_type` for each field | Yes — creates TypeIds in shared table |
| Variant finalization (line 4352) | `finalize_variants` | Yes |
| Private type alias registration (lines 4069-4096) | `define_type_alias` for non-pub aliases | No (module-scoped) |
| Const registration (lines 4140-4191) | `define_const` | Yes — `lookup_const` is global |
| Signature resolution (called at end) | `resolve_program_signatures` | Yes — creates TypeIds |

Because modules are lowered sequentially, **module B's struct/variant/interface
declarations are not yet in the TypeTable when module A is being lowered** (if A
is processed first). Any cross-module type reference during A's lowering that
touches B's nominal types will fall through to `ensure_named` and create a
FORWARD_NOMINAL.

### Where FORWARD_NOMINAL creation happens

In `resolve_opaque_type` (`type_resolve_common.py:25-296`), a FORWARD_NOMINAL
is created in two paths:

1. **Generic nominal with unknown base** (line 182): if a type like `Foo<Bar>`
   is encountered and `Foo` has no declared base in the TypeTable yet,
   `table._add(TypeKind.FORWARD_NOMINAL, name, arg_ids, ...)` is called.

2. **Non-generic nominal fallthrough** (line 286): if a non-generic name like
   `Foo` is not found via `get_nominal(STRUCT/VARIANT/INTERFACE)`, it falls
   through to `ensure_named(name, module_id=origin_mod)` which creates a
   FORWARD_NOMINAL if the name doesn't already exist.

Both paths are reached during struct field TypeId filling (line 4345:
`resolve_opaque_type(f.type_expr, type_table, module_id=module_id)`) and during
signature resolution.

### What categories of bugs can still arise

| Bug class | Mechanism | Example |
|---|---|---|
| **TypeId divergence in generic instantiation** | `List<Foo>` resolved in module A gets `FORWARD_NOMINAL(Foo)` as arg; later `List<Foo>` in B gets concrete `STRUCT(Foo)`. Two different TypeIds for semantically identical type. | The drift-web `add_route` bug (fixed for aliases, but still possible for direct struct references across modules) |
| **Overload resolution failure** | Call resolver matches on TypeId equality. If caller and callee signatures contain divergent TypeIds for the same type, overloads fail to match. | `fn handle(r: mod_b.Request)` vs `fn call(cb: fn(Request))` where Request resolves differently |
| **Interface conformance mismatch** | Interface parent resolution uses `require_nominal(INTERFACE, ...)`. If the parent interface is in a not-yet-lowered module, it fails or creates a wrong TypeId. | Multi-module interface hierarchies with cross-module parents |
| **Struct field type divergence** | Struct field TypeIds filled during lowering (line 4345) can create FORWARD_NOMINALs for cross-module field types. Later generic instantiation of the struct carries the wrong field TypeIds. | `struct Wrapper { inner: other_module.Thing }` lowered before `other_module` |
| **Generic instantiation cache poisoning** | `ensure_struct_instantiated` caches on `(base_id, tuple(type_args))`. If `base_id` is a FORWARD_NOMINAL, the cache entry keys on the wrong TypeId. | Generic struct from module B instantiated in module A before B is lowered |
| **Star re-export TypeId gap** | Star re-export alias registration happens *after* all modules are lowered (lines 3318-3339). A third module referencing the re-exported name during Phase 2 gets a FORWARD_NOMINAL. | `export { other.* }` where the facade module is consumed before re-export aliases are registered |
| **Const type divergence** | Const declarations resolve their type expression (line 4172: `resolve_opaque_type(c.type_expr, ...)`) during lowering. Cross-module type references in const types hit the same ordering issue. | `const X: other_module.Config = ...` |

---

## 2. Architectural Target

### The clean phase model

Source compilation should follow the same principle that makes package ingress
work: **all surface declarations are registered before any resolution that
creates TypeIds**.

The target is a four-phase model:

```
Phase 0: Parse + name-level surface extraction     (already clean)
Phase 1: Nominal declaration of ALL modules         (needs lifting)
Phase 2: Signature resolution + body lowering        (currently entangled with Phase 1)
Phase 3: Checking                                    (already clean)
```

### Phase 0 — Parse + Surface Names (already exists)

- AST parsing
- Module grouping
- Export interface resolution (pub names, star re-exports)
- Import validation and dependency graph
- Module-qualified type-reference rewriting

No TypeIds are created. Only name-level symbol tables.

### Phase 1 — Nominal Pre-Declaration (the refactor target)

For ALL modules, before any signature resolution or body lowering:

1. **Public type aliases** — already done (0.27.131 fix)
2. **Struct declarations** — `declare_struct` with placeholder fields for every
   module's structs
3. **Interface declarations** — `declare_interface` with type params and parent
   resolution for every module's interfaces
4. **Variant declarations** — `declare_variant` with arm schemas for every
   module's variants
5. **Exception schema registration** — `exception_schemas` populated for all
   modules
6. **Star re-export aliases** — registered before resolution, not after
7. **Private type aliases** — registered for all modules (or deferred to Phase 2
   since they are module-scoped and cannot cause cross-module divergence)

After Phase 1, `resolve_opaque_type` should be able to find every nominal type
from every module in the TypeTable. FORWARD_NOMINAL creation for user-defined
types should be a diagnostic-worthy event, not a normal code path.

### Phase 2 — Resolution + Lowering

For each module (order no longer matters):

1. **Struct field TypeId filling** — `resolve_opaque_type` for field types, now
   against a fully-populated TypeTable
2. **Variant finalization** — concrete arm types resolved
3. **Const registration** — type expressions resolved
4. **Function signature resolution** — `resolve_program_signatures`
5. **HIR body lowering** — AST-to-HIR conversion
6. **Trait requirement resolution**

Since all nominal bases are already declared, `resolve_opaque_type` will find
struct/variant/interface bases on first lookup and never fall through to
`ensure_named` for user-defined types.

### Phase 3 — Checking (already clean)

`SemanticWorld.assert_ready()` → type checker runs on complete HIR with
resolved signatures. No changes needed.

### Specifically around each concern

**Nominal declarations**: must be registered for ALL modules before ANY module
resolves TypeIds. This is the core invariant. `declare_struct`,
`declare_interface`, `declare_variant` are lightweight (no TypeId resolution
needed for the declaration itself — only name + field names + type param names).

**Public type aliases**: already pre-registered (0.27.131). The pattern should
be extended to the other nominal kinds.

**Re-exports / facade-visible symbols**: star re-export alias registration must
move from post-Phase-2 to Phase 1. The export interface resolution (Phase 0)
already knows the re-export targets — the aliases just need to be created
before any resolution.

**Signature type resolution**: must happen after all nominal declarations.
Currently happens inside `_lower_parsed_program_to_hir` alongside nominal
declaration. These need to be separated.

**Body checking/lowering**: AST-to-HIR conversion doesn't create TypeIds (it
produces HIR nodes that reference names, not TypeIds). It can stay in Phase 2.
The key is that `resolve_program_signatures` (which does create TypeIds) must
run after Phase 1 is complete for all modules.

---

## 3. Remaining Risk Areas

Beyond type aliases, the following specific paths can still materialize
provisional identities:

### 3.1 Cross-module struct field types

`_lower_parsed_program_to_hir` line 4345:
```python
ft = resolve_opaque_type(f.type_expr, type_table, module_id=module_id)
```

If struct `A.Wrapper` has a field of type `B.Thing`, and module A is lowered
before module B, `B.Thing` will be a FORWARD_NOMINAL. The struct field TypeId
array will contain this provisional identity, and generic instantiation of
`Wrapper<...>` may cache incorrectly.

**Risk**: HIGH for projects with cross-module struct composition.

### 3.2 Cross-module interface parents

`_lower_parsed_program_to_hir` line 4250:
```python
base_id = type_table.require_nominal(
    kind=TypeKind.INTERFACE,
    module_id=parent_mod,
    name=pexpr.name,
)
```

`require_nominal` will fail if the parent interface's module hasn't been
lowered yet. This is currently masked because most interface hierarchies are
within a single module or within stdlib (which is lowered first as a package).

**Risk**: MEDIUM — becomes HIGH if multi-module interface hierarchies grow.

### 3.3 Star re-export resolution timing

Lines 3318-3339 register star re-export aliases AFTER all modules are lowered.
If module C uses `facade.TypeName` during Phase 2, and `facade` star-re-exports
from `origin`, C gets a FORWARD_NOMINAL. The post-lowering alias registration
doesn't retroactively fix TypeIds already created in signatures.

The post-lowering `_coerce_forward_nominal` sweep (line 3916) partially
mitigates this by patching signatures, but:
- It only patches `param_type_ids` and `return_type_id` in signatures
- It does NOT patch TypeIds inside struct field arrays, variant arm types,
  generic instantiation caches, or HIR nodes
- It is recursive but only for REF, ARRAY, FNRESULT, FUNCTION wrappers — not
  for struct/variant instantiation type args

**Risk**: HIGH for facade-pattern projects.

### 3.4 Exception schema ordering

Exception schemas are registered per-module during lowering (line 4036-4047).
If module A's signature references module B's exception type before B is
lowered, `resolve_opaque_type` line 232-235 won't find it in
`exception_schemas` and will create a FORWARD_NOMINAL instead of returning
`ensure_error()`.

The post-lowering coercion handles this specific case (line 3923:
`fqn in shared_type_table.exception_schemas`), but only in signatures, not
in struct fields or generic type args.

**Risk**: LOW (exceptions in field types are rare), but architecturally wrong.

### 3.5 Const type expressions

Line 4172: `resolve_opaque_type(c.type_expr, type_table, module_id=module_id)`.
If a const's type annotation references a cross-module nominal, the same
ordering issue applies.

**Risk**: LOW (const types are usually builtins), but no different from the
struct field case.

### 3.6 Generic instantiation cache coherence

`types_core.py` caches instantiations by `(base_id, tuple(type_args))`. If
`base_id` is a FORWARD_NOMINAL (because the struct/variant wasn't declared yet),
the cache key is wrong. Later, when the real struct base is declared, a
different cache key `(real_base_id, tuple(type_args))` produces a different
instantiation TypeId. This means the same semantic type has two TypeIds.

**Risk**: HIGH — this is the root cause of the TypeId divergence class.

### 3.7 `_coerce_forward_nominal` coverage gaps

The post-lowering sweep (lines 3916-3963) only walks:
- `sig.param_type_ids`
- `sig.return_type_id`
- And recursively into REF, ARRAY, FNRESULT, FUNCTION wrappers

It does NOT walk:
- Struct field type arrays
- Variant arm field types
- Generic instantiation type arguments in the cache
- TypeIds embedded in HIR nodes
- Trait requirement type expressions

This means the sweep is a partial mitigation, not a complete fix.

---

## 4. Phased Implementation Plan

### Phase A: Lift nominal declarations out of per-module lowering

**Goal**: All struct, interface, and variant declarations are registered in the
shared TypeTable for ALL modules before any module begins signature resolution
or field type filling.

**Approach**: Extract the declaration logic from `_lower_parsed_program_to_hir`
into a new pre-pass that runs between the existing Phase 1 (pub type alias
pre-registration) and Phase 2 (per-module lowering). The per-module lowering
function still handles field type filling, signature resolution, and body
lowering.

**Likely files/subsystems**:
- `parser/__init__.py`: new loop over `merged_programs` between lines 3253 and
  3255 that calls `declare_struct`, `declare_interface`, `declare_variant` for
  every module
- `parser/__init__.py` (`_lower_parsed_program_to_hir`): guard the existing
  `declare_struct`/`declare_interface`/`declare_variant` calls to skip if
  already declared (idempotent or skip-if-exists)

**Expected regressions/tests**:
- All existing e2e tests must pass unchanged (the behavior should be identical
  for projects where module ordering didn't matter)
- New regression: multi-module project where module A's struct has a field of
  type `B.Foo` and module A is lowered before B — currently produces
  FORWARD_NOMINAL in field types, should produce concrete TypeId after fix
- New regression: cross-module interface parent resolution where parent is in a
  later-lowered module

**Key risks**:
- Interface parent resolution (`require_nominal` at line 4250) currently
  happens inside `declare_interface`. If we pre-declare interfaces for all
  modules, parent resolution still needs all parents to be declared first. May
  need two sub-passes: (1) declare all interfaces by name, (2) resolve parents.
- Variant arm field types reference structs/interfaces — but `declare_variant`
  doesn't resolve field TypeIds (it uses `GenericTypeExpr` schemas), so this
  should be safe.
- Exception schema registration is currently done inside
  `_lower_parsed_program_to_hir` before struct declarations. Needs to move to
  the pre-pass too, or at least before any `resolve_opaque_type` call.

**Size**: ~100-150 lines of extraction + ~20 lines of guard logic in existing
function. Small, focused.

---

### Phase B: Move star re-export alias registration before lowering

**Goal**: Star re-export type aliases are registered in the TypeTable before any
module is lowered, not after.

**Approach**: Move lines 3318-3339 (star re-export alias registration) to
immediately after the Phase 1 pub type alias pre-registration block. The export
interface resolution (Phase 0) already computes
`reexported_type_targets_by_module`, so the data is available.

**Likely files/subsystems**:
- `parser/__init__.py`: move the re-export alias registration block

**Expected regressions/tests**:
- Existing star re-export tests must pass
- New regression: three-module project where module C references
  `facade.TypeName`, facade star-re-exports from origin, and C is lowered
  before facade's re-export aliases would have been registered

**Key risks**:
- Minimal. The re-export targets are already computed. The only question is
  whether any code between the current location and the new location depends on
  re-export aliases NOT being present. Review the Phase 2 lowering loop for any
  such assumptions.

**Size**: ~15 lines moved, ~10 lines of new test. Very small.

---

### Phase C: Narrow the `_coerce_forward_nominal` sweep

**Goal**: After Phases A and B, the post-lowering FORWARD_NOMINAL coercion
sweep should handle strictly fewer cases. Audit what it still catches, and
either (a) fix the remaining sources so the sweep has nothing to do, or
(b) document the remaining cases as known and add assertions.

**Approach**:
1. Add a counter/log to `_coerce_forward_nominal` that tracks how many
   FORWARD_NOMINALs it actually coerces in practice
2. Run the full test suite and identify which test cases still produce
   FORWARD_NOMINALs that need coercion
3. For each remaining case, determine if it's a legitimate late-binding
   scenario (e.g., stdlib types not declared in source) or a pre-declaration
   gap that should be fixed
4. Add an assertion mode (debug flag) that treats any user-defined
   FORWARD_NOMINAL surviving to signature resolution as a diagnostic

**Likely files/subsystems**:
- `parser/__init__.py`: `_coerce_forward_nominal` function
- Possibly `type_resolve_common.py`: add a diagnostic path when
  FORWARD_NOMINAL is created for a type that should have been pre-declared

**Expected regressions/tests**:
- No behavior changes — this is an audit + assertion phase
- May surface latent ordering bugs in existing tests

**Key risks**:
- The coercion sweep may be load-bearing for cases we don't yet know about.
  The counter/audit approach de-risks this by measuring before removing.

**Size**: ~30 lines of instrumentation, investigation-heavy.

---

### Phase D: Harden `resolve_opaque_type` against provisional identity creation

**Goal**: Make FORWARD_NOMINAL creation for user-defined nominals an explicit,
auditable event rather than a silent fallback.

**Approach**: In `resolve_opaque_type` (`type_resolve_common.py`), the two
FORWARD_NOMINAL creation paths (line 182 for generic nominals, line 286 for
non-generic fallthrough) should be guarded:

1. Before creating a FORWARD_NOMINAL, check if the module is a known
   source module (i.e., in the current compilation's module set). If so, the
   type should already be declared (post Phase A). Creating a FORWARD_NOMINAL
   for a known source module type is now a signal of a pre-declaration gap.
2. Add an optional diagnostic callback / debug flag that logs these events.
3. Keep FORWARD_NOMINAL creation for package-provided types that are
   legitimately forward-referenced (if any such cases exist) and for stdlib
   builtins.

**Likely files/subsystems**:
- `type_resolve_common.py`: guard logic around lines 182 and 286
- `parser/__init__.py`: pass the set of known source module IDs into the
  resolution context

**Expected regressions/tests**:
- No behavior changes for correct code
- New assertion/diagnostic tests for the debug flag path

**Key risks**:
- Must not break package-mode compilation, which legitimately uses
  FORWARD_NOMINAL for cross-package forward references during linking.
  The guard must be source-mode-only.

**Size**: ~40 lines of guard logic + ~20 lines of test.

---

### Phase E (optional): Remove `_coerce_forward_nominal` for source-defined types

**Goal**: Once Phases A-D are complete and validated, the post-lowering coercion
sweep should have nothing to do for source-defined types. Remove it or reduce it
to a package-only / stdlib-only concern.

**Approach**: Replace the sweep with an assertion that no source-defined
FORWARD_NOMINALs remain in signatures. Keep the sweep only for
package/external types if needed.

**Likely files/subsystems**:
- `parser/__init__.py`: replace `_coerce_forward_nominal` with assertion

**Expected regressions/tests**:
- All existing tests must pass with the assertion enabled
- If any test fails, it reveals a gap in Phases A-D

**Key risks**:
- This is the "prove it works" phase. Should only be attempted after Phases
  A-D are validated with real downstream projects (drift-web, net.tls).

**Size**: ~20 lines changed. But depends on all prior phases.

---

## 5. What Should NOT Be the Strategy

### No late canonicalization sweeps as architecture

The `_coerce_forward_nominal` post-lowering sweep is a mitigation, not a
design. It patches over ordering problems after the fact. It has coverage
gaps (struct fields, generic caches, HIR nodes). The correct fix is to
prevent FORWARD_NOMINALs from being created for types that should already
be declared. Post-hoc sweeps should be assertions, not load-bearing
infrastructure.

### No package-path masking

The Phases 1-10 refactor removed old-package fallback and made `canonical_keys`
authoritative. Source mode should follow the same principle: type identity is
established by declaration, not by late path-based matching.

### No stdlib/source special cases

The current code has no explicit stdlib special-casing in the ordering logic
(stdlib is either a package or source modules in the same pass). This should
remain true. The pre-declaration pass must treat all source modules uniformly.

### No hacks that postpone the ordering problem

Specifically:
- No "just sort modules in dependency order" — this doesn't work for circular
  dependencies (which Drift allows) and is fragile
- No "run lowering twice" — wasteful and doesn't solve cache coherence
- No "lazy type table with deferred resolution" — adds complexity without
  removing the ordering sensitivity, just hides it
- No "merge FORWARD_NOMINAL TypeIds with concrete TypeIds post-hoc" — this is
  what `_coerce_forward_nominal` does and it's incomplete by design

The correct approach is the same one that works for packages: declare everything
before resolving anything.

---

## Summary

| Phase | Goal | Size | Risk |
|---|---|---|---|
| **A** | Lift struct/interface/variant declarations to pre-pass | Medium | Interface parent ordering |
| **B** | Move star re-export aliases before lowering | Small | Minimal |
| **C** | Audit `_coerce_forward_nominal` coverage | Investigation | May surface latent bugs |
| **D** | Harden `resolve_opaque_type` against provisional creation | Small | Must not break package mode |
| **E** | Remove sweep for source types (assertion-only) | Small | Depends on A-D validation |

Phases A and B are the structural fixes. Phase C is the validation. Phase D is
the hardening. Phase E is the cleanup.

A and B can be done independently. C depends on A+B. D depends on C. E depends
on D.

Each phase is individually reviewable and testable. No phase requires modifying
the checker, codegen, or package infrastructure. All changes are within
`parser/__init__.py` and `type_resolve_common.py`.
