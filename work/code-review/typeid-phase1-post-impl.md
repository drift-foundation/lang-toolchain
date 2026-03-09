# TypeId Normalization Phase 1 — Post-Implementation Note

**Date**: 2026-03-09
**Status**: Accepted and verified

---

## 1. Workaround/Reconciliation Site Status After Phase 1

### Directly narrowed (confirmed by implementation)

| # | Site | File:Lines | Status | Evidence |
|---|------|-----------|--------|----------|
| 1 | pkg_sigs_by_id return_type_id reconciliation | driftc.py:1650-1669 | **Narrowed** | External sigs now prefer tid_map-based ids for concrete (non-generic) signatures; reconciliation triggers less often because external sig TypeIds are no longer minted fresh by resolve_opaque_type when tid_map already provides concrete ids |
| 2 | `_canonicalize_signature_type_ids` (base_signatures_by_id) | driftc.py:7526 | **Narrowed** | Post-link sweep resolves cross-package FORWARD_NOMINAL keys to concrete host TypeIds; fewer FN entries survive into base_signatures_by_id |
| 3 | `_canonicalize_signature_type_ids` (compile_stubbed_funcs) | driftc.py:6383 | **Narrowed** | External sigs are pre-canonicalized after construction (driftc.py:7847); fewer FN entries reach compile_stubbed_funcs |

### Still fully needed

| # | Site | File:Lines | Why still needed |
|---|------|-----------|------------------|
| 4 | `_canonicalize_struct_field_type_ids` | driftc.py:6395 | Struct field FN sources include resolve_opaque_type in non-signature contexts (checker type resolution, generic instantiation). Phase 1 does not touch these paths. |
| 5 | `_canonicalize_mir_type_ids` | driftc.py:6389 | MIR FN sources include local type resolution and generic instantiation, not exclusively linker-originated. Linker sweep reduces but does not eliminate. |
| 6 | FnInfo resync after canonicalization | driftc.py:6396-6409 | Safety net for any FnInfo ↔ signature divergence after canonicalization. Needed as long as check_by_id copies return_type_id by value before canonicalization runs. |
| 7 | Checker-level FN resolution (K26) | checker/__init__.py:2476-2576 | Checker creates FORWARD_NOMINAL locally during type resolution (not linker-originated). Phase 1 link-time resolution does not affect checker-internal FN creation. |
| 8 | type_subst.py FN handler | type_subst.py:70-78 | Generic substitution receives FORWARD_NOMINAL from checker-level type resolution. Same root cause as #7. |
| 9 | FORWARD_NOMINAL canonical key fix | type_table_link_v0.py:728-745 | Same-package FN→concrete unification in canonical keys. Orthogonal to Phase 1's cross-package sweep. |
| 10 | Struct schema init ordering | type_table_link_v0.py:1028-1035 | Structural ordering fix, unrelated to FORWARD_NOMINAL. |
| 11 | `_remap_mir_func_typeids` + validation | driftc.py:520-731 | Structural necessity for package consumption. Unaffected by Phase 1. |

### Net accounting

- **3 sites directly narrowed** (fewer trigger paths, confirmed)
- **8 sites still fully needed** (not claimable as narrowed by this slice)
- **0 sites retired**

---

## 2. What Phase 1 Actually Changed

| Change | Mechanism | Scope |
|--------|-----------|-------|
| Post-link FN sweep | Builds `(module_id, name) → host_tid` index from concrete nominal keys in `key_to_host`; resolves surviving FORWARD_NOMINAL keys to concrete counterparts | `type_table_link_v0.py` — package-scope-safe, uses linker's own canonical key map |
| tid_map preference | For non-generic external signatures, tid_map-remapped TypeIds are authoritative; `resolve_opaque_type` used only as fallback when DMIR lacks numeric ids or tid_map id is FORWARD_NOMINAL | `driftc.py` — external sig construction (~line 7770) |
| Post-construction canonicalization | `_canonicalize_signature_type_ids(external_signatures_by_id, type_table)` after all external sigs are built | `driftc.py` — safety net (~line 7847) |
| Debug assertions | `AssertionError` raised under `DRIFT_DEBUG_TYPEID_DIVERGENCE=1` at all_sig_env assembly and post-resync | `driftc.py` — two assertion sites |
| Driver regression | `test_ext_sig_preserves_linked_typeids` — builds package with `Wrapper<T>`, consumes it, verifies no divergence under debug mode | `test_external_consumer.py` |

---

## 3. Next Highest-Value Structural Follow-Up

### Analysis of remaining divergence sources

After Phase 1, the remaining FORWARD_NOMINAL creation sites are:

1. **`resolve_opaque_type` (type_resolve_common.py:173)**: Creates FORWARD_NOMINAL when resolving type expressions for unknown generic nominals. Called from checker type resolution, not just external sig construction. This is the primary remaining FN source.

2. **`ensure_named` (types_core.py:470-483)**: Creates FORWARD_NOMINAL when no concrete type exists for a name. Called from checker, type resolution, and (now reduced) linker paths.

3. **Checker-internal FN creation (checker/__init__.py)**: During type resolution, the checker may call `ensure_named` or `resolve_opaque_type` before the concrete type declaration is processed. This produces FORWARD_NOMINAL TypeIds that propagate into signatures, MIR, and struct fields.

The fundamental issue: FORWARD_NOMINAL exists because the checker processes types **before** all declarations are available. In a single-pass architecture, forward references are inevitable. The current multi-pass canonicalization cleans them up after the fact.

### Two candidate next slices

**Candidate A: Eliminate FORWARD_NOMINAL from post-checker output**

Force the checker to resolve all FORWARD_NOMINAL TypeIds to concrete counterparts before emitting FnInfo/signatures. This would eliminate sites #4, #5, #7, #8 from the workaround table.

- Pros: Eliminates the largest remaining bug class (checker-originated FN). Removes 4 workaround sites.
- Cons: Requires checker ordering changes. Some forward references may be structurally unresolvable at checker time (circular dependencies).

**Candidate B: Unify signature construction paths**

Eliminate Path A (external_signatures_by_id via resolve_opaque_type) entirely. Have all external signature TypeIds come exclusively from tid_map, with resolve_opaque_type used only for TypeExpr metadata (param_types/return_type objects, not TypeIds). This would make Path A and Path B structurally identical for TypeId purposes.

- Pros: Eliminates the root cause of Path A/B divergence. Simpler code.
- Cons: Narrower scope — only affects external signature construction, not checker-originated FN.

### Recommendation: Candidate B (signature path unification)

**Rationale**: Candidate A requires touching the checker's type resolution ordering, which is high-risk and broad. Candidate B is tightly scoped, directly eliminates the divergence source that Phase 1 mitigated but did not remove, and makes the external signature path structurally identical to the consumer signature path.

---

## 4. Phase 2 Proposal: Signature Path Unification

### Objective

Eliminate `resolve_opaque_type` as a TypeId source in external signature construction. All external signature TypeIds come from tid_map. `resolve_opaque_type` is retained only for constructing TypeExpr metadata objects (param_types, return_type) used by the checker for generic template resolution — but its TypeId output is never stored in param_type_ids or return_type_id.

### Changes

#### Change 1: Decouple TypeExpr metadata from TypeId assignment in external sig construction

**File**: `lang/driftc/driftc.py`, lines ~7758-7800 (external signature construction loop)

**Current state**: When DMIR provides both serialized TypeIds and TypeExpr strings, Phase 1 added a guard: prefer tid_map ids for non-generic signatures, use resolve_opaque_type for generic templates. This leaves resolve_opaque_type as a TypeId source for generic templates.

**Fix**: For ALL signatures (generic and non-generic):
- TypeIds (`param_type_ids`, `return_type_id`) come exclusively from tid_map remapping of DMIR serialized ids
- TypeExpr objects (`param_types`, `return_type`) are decoded from DMIR strings via `decode_type_expr` (no resolve_opaque_type call) and stored as metadata only
- resolve_opaque_type is called only when DMIR lacks serialized numeric TypeIds entirely (legacy/fallback)

**Prerequisite**: Verify that all current DMIR producers (stdlib package builder, deploy pipeline) serialize numeric TypeIds for all signatures. If any DMIR omits numeric ids, the fallback path must remain.

**Invariant**: After external signature construction, `sig.param_type_ids` and `sig.return_type_id` are always tid_map-remapped values. `sig.param_types` and `sig.return_type` are TypeExpr metadata that may reference type variable names but do not influence TypeId assignment.

#### Change 2: Remove Phase 1's conditional guard

**File**: `lang/driftc/driftc.py`, lines ~7770-7800

Once Change 1 is in place, the `_has_type_params` conditional guard from Phase 1 becomes dead code. The entire resolve_opaque_type-as-TypeId-source block can be removed for the non-fallback case.

#### Change 3: Extend driver test to verify generic template signatures

**File**: `lang/tests/driver/test_external_consumer.py`

Extend `test_ext_sig_preserves_linked_typeids` (or add a sibling) with a package that exports a generic method (e.g., `fn identity<T>(x: T) -> T`). Verify under `DRIFT_DEBUG_TYPEID_DIVERGENCE=1` that the external signature's param_type_ids and return_type_id match tid_map values, not resolve_opaque_type values.

### Files touched

- `lang/driftc/driftc.py`: external sig construction (~30 lines changed)
- `lang/tests/driver/test_external_consumer.py`: test extension (~20 lines)

### Invariants introduced

1. External signature TypeIds are always tid_map-derived (never resolve_opaque_type-derived) when DMIR provides serialized numeric ids
2. resolve_opaque_type is only used as a TypeId source when DMIR lacks numeric ids (legacy fallback, expected to be empty for all current producers)

### Expected bug classes reduced

- **Eliminated**: Path A/B TypeId divergence for generic template signatures (the gap Phase 1 mitigated but left open)
- **Eliminated**: resolve_opaque_type creating FORWARD_NOMINAL TypeIds in external signatures
- **Eliminated**: ensure_*_instantiated cache key divergence from resolve_opaque_type using different base_id than linker

### Workaround sites affected

| # | Site | Expected impact |
|---|------|-----------------|
| 1 | pkg_sigs_by_id reconciliation (driftc.py:1650-1669) | **May narrow further** — fewer divergent TypeIds entering pkg_sigs_by_id |
| 3 | `_canonicalize_signature_type_ids` (compile_stubbed_funcs) | **May narrow further** — external sigs carry pure tid_map ids |
| Post-construction canonicalization (driftc.py:7847) | **May become no-op** — if all external sig TypeIds are tid_map-derived, no FN to canonicalize |

### Stop boundary

This slice does NOT include:
- Eliminating FORWARD_NOMINAL as a TypeKind
- Changing checker-internal type resolution ordering
- Removing `_canonicalize_struct_field_type_ids` or `_canonicalize_mir_type_ids` (checker-originated FN)
- Removing `_canonicalize_signature_type_ids` (retained as safety net)
- Changes to type_table_link_v0.py or types_core.py

### Prerequisite check before starting

Before implementing, verify DMIR serialization: inspect 2-3 DMIR payloads from the current stdlib package builder to confirm all signatures include serialized `param_type_ids` and `return_type_id` fields. If any signatures omit these fields, document which ones and why, since the fallback path must handle them.
