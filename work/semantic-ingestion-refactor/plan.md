# Source Semantic Ingestion / Ordering Refactor

## Status: proposal (review before execution)
## Updated: 2026-04-01 (post-0.27.135 bug cycle)

---

## 0. Overarching Problem: Mode Divergence

Both this plan and the drop-insertion refactor plan address the same
underlying design problem: **the compiler has different semantic pipelines
for different invocation modes** (source vs package vs PEX), and later
passes compensate for the differences with heuristics that keep breaking.

What "mode" should affect:
- Artifact ingress (how types/signatures/MIR enter the pipeline)
- Packaging (what gets serialized into .dmp files)

What "mode" must NOT affect:
- When type identity is resolved
- When `has_drop` / `is_destructible` answers become stable
- When ownership facts are available
- What drops are emitted and where

The concrete bugs that prove this is real:
- **0.27.131**: FORWARD_NOMINAL divergence in source mode because pub type
  aliases were registered per-module instead of upfront
- **0.27.132-0.27.135**: double-drop / missing-drop regressions because
  `has_drop()` returned different answers at different pipeline stages,
  depending on whether stdlib was source or package

The goal of this refactor is: **after artifact ingress, the compiler sees
one unified TypeTable with all nominal declarations, regardless of whether
types came from source modules, co-compiled packages, or external packages.**

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
   iteratively until convergence. Genuine surface-symbol pre-pass, names only.

4. **Import validation and dependency graph** (lines 2389-2634): imports are
   checked against export interfaces. Module dependency edges are computed.
   Trait scopes (`use trait`) are resolved. All symbol-level, no type identity.

5. **Module-qualified type-reference rewriting** (lines 2639-2918):
   `module_alias.TypeName` references in type expressions are rewritten to
   canonical `(module_id, name)` pairs using the import/alias tables.

6. **Package ingress** (handled by `driftc.py` before
   `parse_drift_workspace_to_hir`): all package types are linked into the
   shared TypeTable via `import_type_tables_and_build_typeid_maps` before any
   source parsing. Package type identity is fully resolved, deterministic, and
   order-independent (Phase A/B canonical key system in
   `type_table_link_v0.py`).

7. **Pub type alias pre-registration** (lines 3233-3253, added in 0.27.131):
   all public type aliases from all modules are registered in the shared
   TypeTable before any module is lowered.

### What is still order-sensitive

**Phase 2** (lines 3255-3301): per-module lowering. Each module is lowered
sequentially via `_lower_parsed_program_to_hir`, and inside that function
struct/interface/variant declarations, field type filling, and signature
resolution all happen against the shared TypeTable. Module B's declarations
are not yet in the TypeTable when module A is being lowered.

### Where mode divergence manifests in this subsystem

| Concern | Source mode | Package mode |
|---------|------------|--------------|
| Nominal declarations | Registered during per-module lowering (order-sensitive) | Pre-linked by `type_table_link_v0` (order-independent) |
| Type aliases | Pre-registered (0.27.131 fix) | Pre-linked |
| Star re-exports | Registered AFTER all modules lowered | Pre-linked |
| FORWARD_NOMINAL creation | Possible during lowering (incomplete TypeTable) | Never (TypeTable complete before source parsing) |
| Post-lowering coercion | `_coerce_forward_nominal` sweep (partial mitigation) | Not needed |

The target: source mode should have the same property as package mode —
**all nominal declarations visible before any resolution that creates
TypeIds.**

---

## 2. Architectural Target

### The clean phase model

```
Phase 0: Parse + name-level surface extraction     (already clean)
Phase 1: Nominal declaration of ALL modules         (needs lifting)
Phase 2: Signature resolution + body lowering        (currently entangled with Phase 1)
Phase 3: Checking                                    (already clean)
```

After Phase 1 completes, `resolve_opaque_type` should find every nominal
type from every module in the TypeTable. FORWARD_NOMINAL creation for
user-defined source types should be a diagnostic, not a normal code path.

This directly parallels the package ingress model: canonical keys are
computed from the complete type table, not incrementally during module
processing.

### Specific concerns

**Nominal declarations**: `declare_struct`, `declare_interface`,
`declare_variant` for ALL modules before ANY resolution.

**Public type aliases**: already pre-registered (0.27.131).

**Re-exports / facade symbols**: star re-export alias registration must
move from post-Phase-2 to Phase 1.

**Signature type resolution**: must happen after all nominal declarations.
Currently entangled with declaration inside `_lower_parsed_program_to_hir`.

**Body checking/lowering**: AST-to-HIR doesn't create TypeIds. Can stay in
Phase 2.

---

## 3. Remaining Risk Areas

### 3.1 Cross-module struct field types (HIGH)

`resolve_opaque_type(f.type_expr, ...)` at line 4345 creates
FORWARD_NOMINALs for cross-module field types if the target module hasn't
been lowered yet. Generic instantiation caches on the wrong base_id.

### 3.2 Cross-module interface parents (MEDIUM→HIGH)

`require_nominal(INTERFACE, ...)` at line 4250 fails if the parent
interface's module hasn't been lowered.

### 3.3 Star re-export timing (HIGH)

Star re-export alias registration happens AFTER lowering (lines 3318-3339).
`_coerce_forward_nominal` only patches signatures, not struct fields,
variant arms, or generic caches.

### 3.4 Generic instantiation cache coherence (HIGH)

Cache keys contain FORWARD_NOMINAL TypeIds → divergent instantiations when
the concrete type is later declared.

### 3.5 `_coerce_forward_nominal` coverage gaps

The sweep only walks `param_type_ids` and `return_type_id` with recursive
descent into REF/ARRAY/FNRESULT/FUNCTION. It does NOT walk struct fields,
variant arms, generic instantiation caches, HIR nodes, or trait
requirements.

---

## 4. Phased Implementation Plan

### Phase A: Lift nominal declarations to pre-pass

**Goal**: All struct, interface, and variant declarations registered for ALL
modules before any module resolves TypeIds.

**Approach**: New loop over `merged_programs` between Phase 1 (pub alias
pre-registration) and Phase 2 (per-module lowering). Calls
`declare_struct`, `declare_interface`, `declare_variant` for every module.
Per-module lowering guards declarations to skip-if-already-declared.

**Key sub-structure**: interface parent resolution needs a two-sub-pass
approach: (1) declare all interfaces by name, (2) resolve parents. Variant
arm field types use `GenericTypeExpr` schemas (no TypeId resolution needed
at declaration time).

**Files**: `parser/__init__.py`

**Regressions**:
- Multi-module project where A's struct field references B's type → concrete
  TypeId after fix (not FORWARD_NOMINAL)
- Cross-module interface parent resolution
- Exception schemas moved to pre-pass

**Size**: ~100-150 lines of extraction. Small, focused.

---

### Phase B: Move star re-export aliases before lowering

**Goal**: Star re-export type aliases registered before any module is
lowered.

**Approach**: Move lines 3318-3339 to immediately after Phase 1. Data is
already available from export interface resolution (Phase 0).

**Files**: `parser/__init__.py`

**Regressions**: Three-module facade pattern where C references
`facade.TypeName` before facade's re-export aliases were registered.

**Size**: ~15 lines moved. Very small.

---

### Phase C: Audit and narrow `_coerce_forward_nominal`

**Goal**: After Phases A and B, the sweep should handle fewer cases. Audit,
instrument, and convert to assertion.

**Approach**: Add counter/log, run full test suite, identify remaining
cases, fix sources or document, add assertion mode.

**Files**: `parser/__init__.py`, possibly `type_resolve_common.py`

**Size**: Investigation-heavy, small code changes.

---

### Phase D: Harden `resolve_opaque_type`

**Goal**: FORWARD_NOMINAL creation for known source-module types becomes a
diagnostic, not a silent fallback.

**Approach**: Before creating a FORWARD_NOMINAL, check if the module is in
the current compilation's module set. If so, the type should be declared
(post Phase A). Add optional diagnostic callback.

**Files**: `type_resolve_common.py`, `parser/__init__.py`

**Size**: ~40 lines of guard logic.

---

### Phase E: Remove `_coerce_forward_nominal` for source types

**Goal**: Replace sweep with assertion. Prove Phases A-D eliminated all
source-mode FORWARD_NOMINALs.

**Prerequisites**: Validated with downstream projects.

**Size**: ~20 lines. Depends on A-D.

---

## 5. What Should NOT Be the Strategy

- **No late canonicalization sweeps as architecture**: `_coerce_forward_nominal`
  is a mitigation with known coverage gaps, not a design.
- **No package-path masking**: type identity by declaration, not late matching.
- **No stdlib/source special cases**: all source modules treated uniformly.
- **No hacks postponing the ordering problem**: no dependency-order sorting
  (circular deps allowed), no double lowering, no lazy type table with
  deferred resolution.

---

## 6. Relationship to Drop-Insertion Refactor

These two plans are **anti-mode-divergence plans** addressing the same root
cause:

| Subsystem | Source of divergence | Semantic-ingestion fix | Drop-insertion fix |
|-----------|---------------------|----------------------|-------------------|
| Type identity | Source: order-dependent nominal declaration | Phase A: pre-declare all | N/A |
| `has_drop` stability | Source/PEX: cache poisoned between K39 and MIR lowering | N/A | Phase A: cache clear before lowering |
| Drop decisions | Source/PEX: three independent `_type_needs_drop` functions | N/A | Phases B-D: single authoritative path |
| Post-hoc sweeps | `_coerce_forward_nominal` + `__postdrop_*` | Phases C-E: assertion-only | Phase D: assertion-only |

Both plans converge on the same principle: **facts are established once,
before any downstream consumer runs, and no later pass re-derives or
compensates.**

If both are executed, the compiler's semantic pipeline becomes
mode-independent: source and package ingress produce the same TypeTable
state, the same `has_drop` answers, and the same ownership/drop decisions.
Mode affects inputs and packaging, not compiler meaning.

---

## Summary

| Phase | Goal | Size | Risk |
|-------|------|------|------|
| **A** | Lift struct/interface/variant declarations to pre-pass | Medium | Interface parent ordering |
| **B** | Move star re-export aliases before lowering | Small | Minimal |
| **C** | Audit `_coerce_forward_nominal` coverage | Investigation | May surface latent bugs |
| **D** | Harden `resolve_opaque_type` against provisional creation | Small | Must not break package mode |
| **E** | Remove sweep for source types (assertion-only) | Small | Depends on A-D validation |

A and B are independent. C depends on A+B. D depends on C. E depends on D.
