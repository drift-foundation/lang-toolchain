# Cross-Stage TypeId Normalization — Architectural Report

**Date**: 2026-03-09
**Context**: Wrapper/FnResult reconciliation fix (K-latest) exposed systemic TypeId divergence
**Scope**: Package-consumer compilation path; implications for future C reimplementation

---

## 1. Current-State Diagnosis

### Where logically-identical types diverge in TypeId

The compiler uses a single shared `TypeTable` with `ensure_*_instantiated` methods that **deduplicate** via cache key `(base_id, tuple(type_args))`. The dedup is sound *within a single call path*. Divergence arises when the **same logical type is instantiated through two independent paths that resolve `base_id` or `type_args` to different intermediate TypeIds**.

#### Divergence site A: DMIR decode/remap vs checker fresh instantiation

**Mechanism**: `_build_package_consumer_unit` (driftc.py:1561) decodes DMIR signatures and remaps TypeIds via `tid_map`. The `tid_map` is built by `type_table_link_v0.py` using canonical-key dedup against the shared `TypeTable`. The checker *also* calls `ensure_struct_instantiated(base, args)` on the same `TypeTable` during type resolution.

**Why they can diverge**: The linker calls `ensure_struct_instantiated(base_tid=X, args=[A])` during link-time, allocating TypeId 441. Later, the checker resolves the same logical type but through a different path — e.g., via `resolve_opaque_type` on a FORWARD_NOMINAL, which resolves to the concrete struct base `Y` (a different TypeId than `X` if the linker saw a different canonical entry order). The cache key `(Y, [A])` != `(X, [A])`, so a **second** TypeId 1465 is allocated for the same logical type.

**Concrete example from K-latest**: `Arc<MutexInner<HashMap<String,Int>>>` gets TypeId 441 from DMIR link, TypeId 1465 from checker instantiation. Both are `STRUCT:Arc` with identical structure, but different `base_id` paths produced different cache keys.

**Papered over by**: Reconciliation at driftc.py:1650-1669 (new), `_canonicalize_signature_type_ids` at 6369/7512.

#### Divergence site B: FORWARD_NOMINAL aliases

**Mechanism**: Package imports create FORWARD_NOMINAL TypeDefs as placeholders for types defined in other modules. The concrete type (STRUCT/VARIANT/INTERFACE) exists under a different TypeId. Any code path that stores the FORWARD_NOMINAL TypeId instead of the concrete one creates a parallel identity.

**Scope**: Pervasive. FORWARD_NOMINAL can appear in:
- Signature param_type_ids and return_type_id
- MIR local_types and instruction type fields
- Struct field_types (generic instances)
- Type substitution results (type_subst.py)

**Papered over by**: 6 separate canonicalization passes:
1. `_canonicalize_forward_nominal_type_id` (driftc.py:208) — resolves FN->concrete recursively
2. `_canonicalize_signature_type_ids` (driftc.py:287) — applies (1) to all sigs
3. `_canonicalize_struct_field_type_ids` (driftc.py:297) — applies (1) to struct fields
4. `_canonicalize_mir_type_ids` (driftc.py:323) — applies (1) to MIR instructions
5. Checker `_canonicalize_forward_nominal` (checker/__init__.py:2476) — checker-level resolution
6. `type_subst.py:70-78` — FORWARD_NOMINAL handler in `apply_subst`

#### Divergence site C: Signature reconstruction in `_build_package_consumer_unit`

**Mechanism**: `pkg_sigs_by_id` is built by manually decoding DMIR JSON and applying `tid_map` (driftc.py:1607-1637). This is independent of the checker's FnInfo construction. The signatures carry `tid_map`-remapped TypeIds. When `all_sig_env` is assembled (driftc.py:1941-1945), `checked_src` sigs overwrite `pkg_sigs_by_id` entries for functions the checker processed. But wrapper **targets** (package-internal functions the wrapper calls) may only exist in `pkg_sigs_by_id`, never overwritten — so their return_type_id remains the DMIR-remapped value, diverging from the checker's TypeId used in TypeEnv.

**Papered over by**: Reconciliation at driftc.py:1650-1669.

#### Divergence site D: MIR instruction TypeIds post-remap

**Mechanism**: `_remap_mir_func_typeids` (driftc.py:520-608) remaps all TypeId-bearing fields in decoded MIR. But coverage must be manually maintained for every new MIR instruction type. Missing a field silently leaves a package-space TypeId that may collide with a different type in the host's TypeId space.

**Papered over by**: `_validate_remap_completeness` (driftc.py:659-731) — runtime detection, not prevention.

### Summary of current reconciliation patches

| # | File | Lines | What it fixes | Type |
|---|------|-------|--------------|------|
| 1 | driftc.py | 1650-1669 | pkg_sigs return_type_id vs checker | Local workaround |
| 2 | driftc.py | 208-294 | FORWARD_NOMINAL in signatures | Structural boundary pass |
| 3 | driftc.py | 297-321 | FORWARD_NOMINAL in struct fields | Structural boundary pass |
| 4 | driftc.py | 323-347 | FORWARD_NOMINAL in MIR instructions | Structural boundary pass |
| 5 | driftc.py | 520-608 | Package->host TypeId remap in MIR | Structural necessity |
| 6 | driftc.py | 659-731 | Remap completeness validation | Defensive detection |
| 7 | type_table_link_v0.py | 728-745 | FN canonical key -> concrete kind | Structural fix at link |
| 8 | type_table_link_v0.py | 1028-1035 | Struct schema init ordering | Structural ordering fix |
| 9 | checker/__init__.py | 2476-2576 | Checker-level FN resolution (K26) | Layered workaround |
| 10 | type_subst.py | 70-78 | FN in generic substitution | Structural fix |
| 11 | call_resolver.py | ~2960 | Wrapper redirect type compat | Implicit dependency |

**Total**: 11 reconciliation/fixup sites across 5 files. 5 are structural necessities, 6 are post-hoc workarounds.

---

## 2. Contract Definition

### What the canonical cross-stage type identity contract should be

**Single Source of Truth (SSOT)**: After type-table linking completes, every logical type has exactly one TypeId in the shared TypeTable. No stage should create, store, or propagate a second TypeId for the same logical type.

**Identity invariants**:

1. **Nominal identity**: Two types are the same iff they have the same `(kind, module_id, name)` for base types, or the same `(base_id, tuple(type_args))` for instantiations, where `base_id` and each `type_arg` are themselves canonical.

2. **Package load**: `tid_map` maps every package TypeId to a canonical host TypeId. Post-remap, no package-space TypeId survives in any data structure.

3. **Type remap/link**: `type_table_link_v0.py` canonical keys guarantee that structurally-identical types from different packages merge to the same host TypeId. FORWARD_NOMINAL entries must resolve to concrete types *during linking*, not after.

4. **Checker fresh instantiation**: The checker must not create a fresh instantiation TypeId if the linker already created one for the same logical type. This is currently guaranteed **only if** both paths use the same `(base_id, args)` — which fails when FORWARD_NOMINAL introduces an alternate `base_id`.

5. **Cross-language stage boundaries**: In a future C implementation, the contract must be: TypeIds are stable u32 handles into a global type table. No stage may mint TypeIds outside the type table's allocator. The type table is the sole authority, and all queries go through it.

### What identity must survive

| Transition | Current guarantee | Gap |
|-----------|------------------|-----|
| Package load -> host TypeTable | `tid_map` provides bijection | FORWARD_NOMINAL resolved too late |
| Type remap -> MIR instructions | Manual field-by-field remap | Coverage is manual, fragile |
| Checker instantiation -> codegen | Shared TypeTable cache | Cache key diverges when base_id differs |
| MIR -> SSA -> TypeEnv -> codegen | Sigs carry TypeIds from construction | Wrapper sigs vs target sigs can diverge |

---

## 3. Solution Options

### Option A: Minimal hardening on current architecture

**Approach**: Keep the current multi-pass canonicalization architecture. Add targeted invariant checks and close the remaining gaps.

**Concrete touchpoints**:

1. **Eagerly resolve FORWARD_NOMINAL during link** (type_table_link_v0.py:728-745): Currently resolves only for canonical key computation. Extend to *not emit* FORWARD_NOMINAL TypeDefs into the host TypeTable at all — resolve to concrete types at link time, store the resolution in tid_map directly.
   - File: `type_table_link_v0.py`, canonical key phase + tid_map construction
   - Risk: Low. FORWARD_NOMINAL entries serve no purpose post-link; eliminating them at source removes Divergence B entirely.

2. **Unify pkg_sigs_by_id construction with checker output** (driftc.py:1607-1669): Instead of manually decoding DMIR sigs + applying tid_map, use the checker's FnInfo for all functions the checker processed, falling back to DMIR-decoded sigs only for functions the checker didn't see.
   - File: `driftc.py`, `_build_package_consumer_unit`
   - Risk: Medium. Checker may not process all package functions (e.g., unreachable ones).

3. **Add TypeId equivalence assertion at all_sig_env assembly** (driftc.py:1941): Before overwriting, assert that when both `pkg_sigs_by_id` and `checked_src` provide a sig for the same fn_id, their TypeIds are equivalent (same LLVM layout). Catch future divergences as hard errors.
   - File: `driftc.py`
   - Risk: Low.

4. **Codegen-level TypeId normalization pass**: Before LLVM emission, walk all FnInfos and normalize return_type_id/param_type_ids through a single canonical lookup. This is a final safety net.
   - File: `llvm_codegen.py`, entry point
   - Risk: Low.

**Expected coverage**:
- Eliminates FORWARD_NOMINAL class entirely (touchpoint 1)
- Closes wrapper/target divergence (touchpoint 2)
- Detects future regressions (touchpoint 3)
- Safety net for any remaining gaps (touchpoint 4)

**Migration cost**: ~2-3 focused changes. No architectural restructuring.

**Bug classes eliminated**: All FORWARD_NOMINAL divergences. Wrapper return_type_id mismatches. Does NOT eliminate the fundamental issue that two paths through `ensure_*_instantiated` with different `base_id` can produce different TypeIds.

### Option B: Structural normalization redesign

**Approach**: Introduce a canonical TypeId resolution layer that enforces SSOT at the TypeTable level, making divergence structurally impossible.

**Concrete touchpoints**:

1. **TypeTable canonical normalization table**: Add a `_canonical: dict[TypeId, TypeId]` map to TypeTable. When `ensure_*_instantiated` creates a new TypeId, also register it against a structural key `(kind, module_id, name, tuple(canonical(arg) for arg in args))`. If a structurally-identical TypeId already exists, map the new one to the existing one. All TypeId queries go through `canonical(tid)` before use.
   - File: `types_core.py`, TypeTable class
   - Risk: Medium. Changes the fundamental TypeId contract. All consumers must call `canonical()` or the table must intercept `get()`.

2. **Eliminate FORWARD_NOMINAL from TypeTable entirely**: FORWARD_NOMINAL becomes a transient parsing artifact that is resolved to a concrete TypeId before being stored in the TypeTable. The linker, checker, and all downstream stages never see it.
   - Files: `type_table_link_v0.py`, `types_core.py`, `checker/__init__.py`
   - Risk: Medium-High. FORWARD_NOMINAL currently serves as a deferred-resolution placeholder; eliminating it requires all type resolution to be eager.

3. **Signature TypeIds from TypeTable, not from DMIR decode**: Instead of `_build_package_consumer_unit` manually parsing signature JSON and remapping TypeIds, have the linker produce a `pkg_fn_sigs: dict[FunctionId, FnSignature]` where all TypeIds are already canonical host TypeIds. The consumer path just reads this.
   - Files: `type_table_link_v0.py`, `driftc.py`
   - Risk: Medium. Requires the linker to understand FnSignature structure.

4. **Remove all post-hoc canonicalization passes**: Once (1)-(3) are in place, `_canonicalize_signature_type_ids`, `_canonicalize_struct_field_type_ids`, `_canonicalize_mir_type_ids`, and the reconciliation block become dead code.
   - Files: `driftc.py` (remove 4 functions + call sites)
   - Risk: Low once (1)-(3) are proven correct.

**Expected coverage**: Eliminates all known divergence classes structurally. New MIR instruction types cannot introduce TypeId divergence because TypeIds are canonical at source.

**Migration cost**: Substantial. Touches TypeTable core, linker, checker, and consumer path. Requires comprehensive regression testing.

**Bug classes eliminated**: ALL TypeId divergence bugs, including the fundamental `base_id` divergence that Option A leaves open.

---

## 4. Recommended Direction

### Now (immediate, before next release)

1. **Keep the tactical reconciliation fix** (driftc.py:1650-1669). It's correct, low-risk, and handles the known trigger.

2. **Add an assertion at all_sig_env assembly** that detects TypeId divergence between pkg_sigs and checker sigs as a hard diagnostic rather than silent corruption:
   ```python
   # At driftc.py ~1941, after all_sig_env construction:
   for fn_id, sig in all_sig_env.items():
       if fn_id in pkg_sigs_by_id and fn_id in checked_src.fn_infos_by_id:
           pkg_ret = pkg_sigs_by_id[fn_id].return_type_id
           chk_ret = sig.return_type_id
           if pkg_ret != chk_ret:
               # Log/assert: divergence detected, reconciliation applied
   ```

3. **Make `pkg_wrap_method_fnresult_boundary` a permanent regression test**. Even though it doesn't reproduce the exact failure on current stdlib, it exercises the code path.

### Next milestone (Option A, focused hardening)

4. **Resolve FORWARD_NOMINAL at link time** (touchpoint A.1). This is the highest-value change — it eliminates an entire class of bugs (6 of 11 current patches exist solely for FORWARD_NOMINAL).

5. **Unify sig construction** (touchpoint A.2). Have `_build_package_consumer_unit` prefer checker FnInfo sigs over DMIR-decoded sigs for all functions, not just source functions.

### Deferred (Option B, when C reimplementation begins)

6. **TypeTable canonical normalization**. This is the right design for a C implementation where TypeIds are u32 handles and the type table is a performance-critical data structure. Doing it now in Python adds complexity without proportional benefit — the Python compiler is not performance-bound by TypeId lookups.

7. **Eliminate FORWARD_NOMINAL as a TypeKind**. This is the clean end-state but requires eager type resolution everywhere, which is a significant refactor.

### Assessment

**The final clean solution does NOT require a major rework.** It requires a **focused staged refactor**:
- Stage 1 (now): tactical fix + assertions + permanent regression test
- Stage 2 (next milestone): FORWARD_NOMINAL elimination at link + sig unification
- Stage 3 (C reimplementation): canonical TypeId table

Stage 2 alone would eliminate ~80% of current reconciliation patches. The remaining ~20% (MIR remap completeness) is inherent to the package serialization format and goes away only when the C implementation uses a binary type-table format with pre-resolved TypeIds.

---

## 5. Validation Plan

### Positive parity/boundary tests

1. **`pkg_wrap_method_fnresult_boundary`** (exists): Exercises nothrow wrapper with generic return types (`HashMap.remove` -> `Optional<V>`, `iter.next` -> `Optional<&T>`).

2. **`pkg_iter_next_visibility`** (exists): Iterator interface method visibility across package boundary.

3. **New: `pkg_generic_struct_return_divergence`**: User-package test (not stdlib) where a method returns a generic struct `Result<T>` and the package's TypeId for `Result<Int>` differs from the consumer's TypeId. This would be the first test that actually fails without the reconciliation fix.

4. **Existing ext-e2e suite (553/561)**: Full package-consumer coverage. Run on every change to the consumer path.

### Negative diagnostics for mixed-namespace type identity

5. **TypeId divergence assertion** (recommended above): At all_sig_env assembly, assert that pkg_sigs and checker sigs agree. In debug mode, emit a diagnostic like:
   ```
   internal: TypeId divergence for fn X: pkg_sig return_type_id=441 (STRUCT:Arc) vs checker return_type_id=1465 (STRUCT:Arc)
   ```

6. **Remap completeness validator** (exists, driftc.py:659-731): Already detects unrewritten package TypeIds. Extend to also detect FORWARD_NOMINAL TypeIds that survived into MIR post-canonicalization.

7. **New: post-canonicalization FORWARD_NOMINAL sweep**: After all canonicalization passes, scan all sig TypeIds and MIR TypeIds for any remaining FORWARD_NOMINAL. Emit a hard error if found. This catches any canonicalization gaps immediately.

### Package-consumer regression cases that should become permanent

| Test | What it validates | Status |
|------|------------------|--------|
| `pkg_wrap_method_fnresult_boundary` | Generic return type wrapper boundary | New, should be permanent |
| `pkg_iter_next_visibility` | Iterator trait method visibility | New, should be permanent |
| `pkg_iface_impl_vtable` | Interface vtable across boundary | New, should be permanent |
| `pkg_ext_module_trait_scope` | Trait scope serialization | New, should be permanent |
| `pkg_vis_source_private_method_rejected` | Negative: private method rejection | New, should be permanent |
| `pkg_vis_source_trait_scope_rejected` | Negative: trait scope rejection | New, should be permanent |
| All existing ext-e2e tests | Full boundary coverage | Already permanent |

### Proving the normalization contract holds

The contract "after link, every logical type has exactly one TypeId" can be validated by:

1. **Static check**: After `import_type_tables_and_build_typeid_maps` completes, scan the TypeTable for pairs `(tid_a, tid_b)` where `tid_a != tid_b` but both have `kind=STRUCT/VARIANT/INTERFACE`, same `module_id`, same `name`, and same `param_types` (recursively compared). Any such pair is a contract violation. Cost: O(n^2) on type count, acceptable for debug builds.

2. **Runtime check**: After all canonicalization passes, before codegen, assert that no `FnSignature.return_type_id` or `param_type_ids` entry references a FORWARD_NOMINAL TypeId. Cost: O(sigs * params), negligible.

3. **CI gate**: Add both checks as assertions in the test harness. They run on every ext-e2e test automatically.

---

## Appendix: File Index

| File | Role in TypeId normalization |
|------|------------------------------|
| `lang/driftc/core/types_core.py` | TypeTable, TypeId allocation, instantiation caches |
| `lang/driftc/packages/type_table_link_v0.py` | Canonical key computation, tid_map construction, FORWARD_NOMINAL resolution |
| `lang/driftc/driftc.py` | Consumer path assembly, reconciliation patches, canonicalization passes |
| `lang/driftc/checker/__init__.py` | FnInfo construction, checker-level FN resolution |
| `lang/driftc/checker/call_resolver.py` | Wrapper redirect, variant ctor FN resolution |
| `lang/driftc/core/type_subst.py` | Generic substitution, FN handler |
| `lang/codegen/llvm/llvm_codegen.py` | FnResult TypeId resolution, ConstructResultOk validation |
| `lang/driftc/stage4/throw_checks.py` | FnResult parts comparison (declared vs actual) |
