# TypeId Normalization Phase 1 — Implementation Plan

**Date**: 2026-03-09
**Scope**: FORWARD_NOMINAL link-time resolution + signature unification + divergence assertion
**Constraint**: No broad refactor. Changes attributable to this slice only.

---

## 1. Changes

### Change 1: Resolve FORWARD_NOMINAL at link time in `type_table_link_v0.py`

**Problem**: The linker can create FORWARD_NOMINAL TypeDefs in the host TypeTable at line 1085:
```python
if kind_s == TypeKind.FORWARD_NOMINAL.name:
    key_to_host[k] = host.ensure_named(name, module_id=(mid or None))
```
`ensure_named` first checks for existing concrete types. If none found, it creates a new FORWARD_NOMINAL TypeDef. This happens for cross-package forward references where the referenced type hasn't been declared yet in the host TypeTable.

Additionally, `resolve_opaque_type` (type_resolve_common.py:173) creates FORWARD_NOMINAL TypeDefs for unknown generic nominals during external signature resolution.

**Fix**: Two-pass linking for FORWARD_NOMINAL nominal keys.

1. In the `remaining_keys` processing loop (line 1080-1087): when a FORWARD_NOMINAL nominal key is encountered and `ensure_named` would create a new FORWARD_NOMINAL, instead defer it to a second pass. After all other types are allocated, attempt resolution again — by then, the concrete type from another package should be available.

2. If a FORWARD_NOMINAL still can't resolve after the second pass, keep it as-is (backward compat) but log a debug diagnostic.

**Files**:
- `lang/driftc/packages/type_table_link_v0.py`: lines 1080-1087 (remaining_keys FORWARD_NOMINAL handling)

**Invariant introduced**: After `import_type_tables_and_build_typeid_maps` returns, no host FORWARD_NOMINAL TypeDef should survive when a concrete host definition for the same `(module_id, name)` is available after linking. Same-package FN→concrete unification is already handled by canonical key computation (lines 728-745); this change closes the remaining gap for cross-package orphaned FN entries in `remaining_keys` that arrive before their concrete counterpart is allocated.

**Non-claim**: This does NOT guarantee that every tid_map value for a package FORWARD_NOMINAL becomes concrete. A package FORWARD_NOMINAL referencing a type with no concrete definition in any loaded package will still map to a host FORWARD_NOMINAL. The invariant is scoped to: "no missed resolution when a concrete definition exists."

**Risk**: Low. Deferred FN resolution is additive — the existing `ensure_named` fallback remains for truly unresolvable names. No semantic change for types that already resolve correctly.

### Change 2: Prefer tid_map TypeIds as authoritative; resolve_opaque_type as fallback only

**Problem**: Two independent paths construct signatures for the same package functions:

- **Path A** (driftc.py:7626-7757, external_signatures_by_id): Decodes DMIR payload, starts with `tid_map.get()` remap for param_type_ids/return_type_id/impl_target_type_id. But then OVERRIDES the TypeId at line 7747-7750 and 7756-7757 when raw `param_types`/`return_type` expressions are available — calling `resolve_opaque_type()` which can produce FORWARD_NOMINAL TypeIds or fresh instantiation TypeIds that diverge from tid_map-allocated ones.

- **Path B** (driftc.py:1607-1637, pkg_sigs_by_id): Decodes DMIR payload, applies `tid_map.get()` for all TypeIds. No `resolve_opaque_type` call — uses pure tid_map remapping.

The override in Path A is the primary source of divergence. When `resolve_opaque_type` is called with a type expression, it may create a FORWARD_NOMINAL or hit a different `ensure_*_instantiated` cache key than the linker used, producing a different TypeId than what tid_map provides. The fundamental issue is "construct through a different path, then clean up later" — canonicalization after the fact helps but does not eliminate the divergence source.

**Fix**: Three-layer defense in Path A (driftc.py:7626-7757):

1. **Prefer tid_map-based TypeIds as authoritative.** When DMIR provides serialized `param_type_ids` / `return_type_id` and tid_map successfully remaps them to concrete host TypeIds, those remapped ids are the final answer. Do NOT override them with `resolve_opaque_type` results.

2. **Use `resolve_opaque_type` only as fallback** when DMIR did not serialize numeric TypeIds for a parameter or return type (i.e., only `param_types` / `return_type` TypeExpr strings are available, with no corresponding numeric id). This is the case for older DMIR formats or signatures where the serializer omitted ids.

3. **Canonicalize after construction.** After all external signatures are built, apply `_canonicalize_signature_type_ids(external_signatures_by_id, type_table)` as a safety net for any FORWARD_NOMINALs that slipped through the fallback path.

Concretely, the override block at lines 7747-7750 and 7756-7757 gains a guard: skip the `resolve_opaque_type` override when the tid_map-remapped id is already concrete (i.e., not FORWARD_NOMINAL and not missing from tid_map) AND the signature has no type parameters. For generic templates, `resolve_opaque_type` must still be used because it resolves TypeExpr with type variable bindings that tid_map numeric ids don't carry.

**Files**:
- `lang/driftc/driftc.py`: lines 7747-7757 (guard resolve_opaque_type behind tid_map concreteness check) + after ~line 7800 (add `_canonicalize_signature_type_ids` call)

**Invariant introduced**: After external signature construction, no signature in `external_signatures_by_id` contains a FORWARD_NOMINAL TypeId in param_type_ids, return_type_id, or error_type_id. Additionally, when DMIR provides serialized TypeIds, the external signature preserves the linked/canonical ids rather than diverging through `resolve_opaque_type`.

**Risk**: Low. The guard is a no-op for signatures where tid_map already provides concrete ids (majority case). The fallback path through `resolve_opaque_type` is retained for DMIR payloads missing numeric ids. Post-construction canonicalization is the same mechanism already used for base_signatures_by_id.

### Change 3: Debug divergence assertion at stage boundaries

**Problem**: TypeId divergence between FnInfo, all_sig_env, and TypeEnv is currently silent until it triggers a throw_checks or codegen error.

**Fix**: Add a conditional assertion (gated on `DRIFT_DEBUG_TYPEID_DIVERGENCE=1` env var) at two points:

1. **After all_sig_env assembly** (driftc.py `_build_package_consumer_unit`, ~line 1955): For every fn_id present in both pkg_sigs_by_id and checked_src, assert that their return_type_ids map to the same LLVM layout (same TypeKind, same structural shape). Log divergence with both TypeIds and their TypeDef descriptions.

2. **After FnInfo resync** (driftc.py `compile_stubbed_funcs`, after the resync block at ~line 6395): Scan all FnInfos and assert that `info.return_type_id == info.signature.return_type_id` — any remaining divergence after resync is a bug.

**Files**:
- `lang/driftc/driftc.py`: two assertion blocks (consumer unit + compile_stubbed_funcs)

**Invariant introduced**: In debug/test mode, TypeId divergence between FnInfo and signature is detected and reported at construction time, not deferred to throw_checks or codegen.

**Risk**: Zero for production (env-gated). Low for debug mode (assertion-only, no behavioral change).

---

## 2. Workaround Patches: Expected Impact

Patches are categorized by confidence level: **directly affected** (committed outcome of this slice) vs **may see fewer triggers** (plausible but not guaranteed by this slice alone).

### Directly affected (committed outcomes)

| # | Current patch | File:Lines | After Phase 1 |
|---|--------------|------------|---------------|
| 1 | pkg_sigs_by_id return_type_id reconciliation | driftc.py:1650-1669 | **Narrowed** — fewer triggers because Change 2 keeps external sigs on tid_map-based ids; reconciliation still needed for DMIR-remapped vs checker divergence when target not in checked_src |
| 2 | `_canonicalize_signature_type_ids` (base_signatures_by_id) | driftc.py:7526 | **Narrowed** — fewer FORWARD_NOMINAL entries to canonicalize because Change 1 resolves cross-package FN at link time |
| 3 | `_canonicalize_signature_type_ids` (inside compile_stubbed_funcs) | driftc.py:6383 | **Narrowed** — fewer FORWARD_NOMINAL entries because Change 2 pre-canonicalizes external sigs |

### May see fewer triggers (not guaranteed by this slice)

| # | Current patch | File:Lines | After Phase 1 |
|---|--------------|------------|---------------|
| 4 | `_canonicalize_struct_field_type_ids` | driftc.py:6395 | **May narrow** — fewer host FN survivors from Change 1, but struct field FN sources are not all linker-originated |
| 5 | `_canonicalize_mir_type_ids` | driftc.py:6389 | **May narrow** — fewer FN in MIR, but MIR FN sources include resolve_opaque_type in non-signature contexts |
| 7 | Checker-level FN resolution (K26) | checker/__init__.py:2476-2576 | **May narrow** — fewer FN reaching checker, but checker FN sources include local type resolution paths |
| 8 | type_subst.py FN handler | type_subst.py:70-78 | **May narrow** — fewer FN in substitution inputs, but substitution FN sources are not exclusively linker-originated |

### Retained unchanged

| # | Current patch | File:Lines | After Phase 1 |
|---|--------------|------------|---------------|
| 6 | FnInfo resync after canonicalization | driftc.py:6396-6409 | **Retained** — still needed as a safety net; the resync is correct regardless of canonicalization scope |
| 9 | FORWARD_NOMINAL canonical key fix | type_table_link_v0.py:728-745 | **Retained** — still needed for same-package FN→concrete resolution in canonical keys |
| 10 | Struct schema init ordering | type_table_link_v0.py:1028-1035 | **Retained** — unrelated to FORWARD_NOMINAL; structural ordering fix |

**Net effect**: 0 patches fully retired, 3 patches directly narrowed (committed), 4 patches may see fewer triggers (not guaranteed), 3 patches retained unchanged. Full retirement requires Phase 2 (eliminate FORWARD_NOMINAL as a TypeKind).

---

## 3. Regression Plan

### Positive regressions (package-wrapper / generic-return boundary)

1. **`pkg_wrap_method_fnresult_boundary`** (exists): HashMap.remove returning `Optional<V>`, iter.next returning `Optional<&T>`. Exercises wrapper boundary with generic return types. **Keep permanent.**

2. **New: `pkg_wrap_method_fnresult_forward_nominal`**: Construct a scenario where a package method returns a type that is only known as FORWARD_NOMINAL at external-signature construction time (e.g., a type alias or a struct from a different package module). The wrapper's FnResult must use the canonical TypeId. This test would fail WITHOUT Change 1 + Change 2 applied, because the external sig would carry a FORWARD_NOMINAL TypeId.

   Concrete approach: use a HashMap method returning `Optional<V>` where V is a user-defined struct type (not a primitive). This forces `resolve_opaque_type` to go through the FORWARD_NOMINAL path for the struct if it hasn't been declared yet.

### Driver-level assertion (Change 2 verification)

3. **New driver test: `test_external_sig_preserves_linked_typeids`**: Inspects external signature construction directly. Loads a signed package with a method returning a generic instantiation (e.g., `Optional<UserStruct>`), builds external_signatures_by_id, and asserts that when DMIR provides serialized TypeIds, the resulting external signature keeps the linked/canonical ids (from tid_map) rather than diverging through `resolve_opaque_type`. Specifically:
   - The external sig's return_type_id must equal `tid_map[dmir_return_type_id]`
   - The external sig's return_type_id must NOT be a FORWARD_NOMINAL
   - The external sig's return_type_id must equal the corresponding pkg_sigs_by_id entry's return_type_id (Path A == Path B)

   This catches the Path A vs Path B divergence earlier than a downstream wrapper/codegen failure. Lives in `lang/tests/driver/`.

### Negative / debug assertions

4. **Post-link FORWARD_NOMINAL sweep**: After `import_type_tables_and_build_typeid_maps` returns, scan the host TypeTable for FORWARD_NOMINAL TypeDefs whose `(module_id, name)` matches a concrete STRUCT/VARIANT/INTERFACE already in the table. Any match is a missed resolution — emit a debug diagnostic. Run this sweep in the test harness for all ext-e2e tests.

5. **FnInfo ↔ signature divergence assertion** (Change 3): Runs in debug mode for all ext-e2e tests. Catches any remaining stale FnInfo.return_type_id after resync.

6. **all_sig_env divergence assertion** (Change 3): Runs in debug mode for all pkg-consumer tests. Catches TypeId mismatches between pkg_sigs_by_id and checked_src for the same fn_id.

---

## 4. Execution Order

1. Change 1 (type_table_link_v0.py FORWARD_NOMINAL deferred resolution) — standalone, testable in isolation
2. Change 2 (tid_map preference + resolve_opaque_type fallback guard + post-construction canonicalization) — depends on Change 1 for maximum effect
3. Change 3 (debug assertions) — independent, can go in parallel with 1 or 2
4. Driver test: `test_external_sig_preserves_linked_typeids` — after Change 2, verifies Path A/B convergence directly
5. E2e regression test: `pkg_wrap_method_fnresult_forward_nominal` — after Change 1 + 2 are in place

Total: 3 production files modified (`type_table_link_v0.py`, `driftc.py` x2 locations), 1 new driver test, 1 new e2e test. No changes to `types_core.py`, `checker/__init__.py`, `type_subst.py`, or `llvm_codegen.py`.

---

## 5. Scope Boundary

This slice does NOT include:
- Eliminating FORWARD_NOMINAL as a TypeKind (Phase 2)
- TypeTable canonical normalization table (Phase 3 / C reimplementation)
- Removing existing canonicalization passes (they narrow but remain needed)
- Changes to the checker, type_subst, or codegen
- Refactoring `_build_package_consumer_unit` to share code with the external sig path

If during implementation the linker's deferred FORWARD_NOMINAL resolution proves insufficient (e.g., circular cross-package FN references that can't be resolved in two passes), I will stop and report the blocking issue before expanding scope.
