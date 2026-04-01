# Drop Insertion / Ownership Refactor Plan

## Status: proposal (review before execution)
## Updated: 2026-04-01 (post-0.27.135 bug cycle)

---

## 0. Overarching Problem: Mode Divergence

This plan and the semantic-ingestion refactor plan address the same root
cause: **the compiler has different semantic pipelines for different
invocation modes**, and later passes compensate with heuristics.

The drop-insertion bugs (0.27.132 through 0.27.135) all share a pattern:

1. `has_drop()` returns different answers depending on pipeline timing
2. MIR lowering makes drop decisions using one answer
3. A later pass (string_arc, post-pass) makes decisions using a different answer
4. The disagreement produces double-drops, missed drops, or null-pointer crashes

The concrete trigger is **stdlib as source vs stdlib as package**:
- Source: `has_drop` is stable from the start (all types declared upfront)
- Package/PEX: `has_drop` is unstable — `destructor_fns` is populated
  incrementally by K39, and `_needs_drop_cache` retains stale entries from
  queries between K39 rounds

The 0.27.135 fix (cache clear before MIR lowering) addresses the immediate
symptom. This plan addresses the structural cause: **three independent
drop-checking functions, three independent caches, no explicit ownership
facts propagated between stages.**

---

## 1. Current Drop Pipeline End-to-End

### Stage 1: Type Checker + Borrow Checker (HIR level)

The borrow checker (`borrow_checker_pass.py:134`) computes per-place
ownership state via dataflow: `PlaceState` (UNINIT/VALID/MOVED) per-place
per-CFG-node. Runs at `driftc.py:5438`.

**Problem**: output is diagnostics-only. Ownership facts are discarded.

### Stage 2: HIR-to-MIR Lowering (`hir_to_mir.py`)

Independently re-derives ownership state:
- `_param_drop_locals` (line 303): populated by checking
  `_needs_runtime_drop(ty)` and `_type_is_destructible(ty)` for each param
- `_moved_locals`: tracks locals consumed by `move` expressions
- `_emit_scope_drops` (line 596): emits `MoveOut + DropValue` for in-scope
  locals that need drop and haven't been moved

Drop decisions depend on `has_drop` / `is_destructible` at lowering time.

### Stage 2.5: string_arc Pass (`string_arc.py`)

MIR-to-MIR pass that:
1. Expands `MoveOut` to `LoadLocal + ZeroValue + StoreLocal` (line 807-819)
2. Re-derives drop needs using its own `_type_needs_drop` (line 63-108)
   with its **own cache** — does NOT check `destructor_fns` at field level
3. Emits `_drop_all_destructibles` at return blocks (line 1242)

### Stage 3: Post-Pass Drop Injection (`driftc.py`)

`_postdrop_inject_missing_param_drops` (line 3019):
1. Clears `_needs_drop_cache`
2. Scans MIR for existing drops via instruction pattern matching
3. Injects `__postdrop_*` where no drop is found

**Problem**: pattern-matcher that keeps breaking on new MIR shapes.

### Stage 4: LLVM Codegen (`llvm_codegen.py`)

`_emit_drop_value` (line 8773) trusts every `DropValue` instruction. No
ownership metadata consumed.

---

## 2. The Architectural Seam

### Three independent drop-checking functions

| Stage | Function | `destructor_fns`? | `is_destructible`? | Own cache? |
|-------|----------|-------------------|--------------------|-----------:|
| HIR-to-MIR | `_needs_runtime_drop` | via `has_drop` | Yes | TypeTable |
| string_arc | `_type_needs_drop` | **No** | Yes | **Own** |
| Post-pass | `has_drop` | Yes | Yes | TypeTable (cleared) |

### Proven bug classes from this divergence

| Version | Bug | Root cause |
|---------|-----|-----------|
| 0.27.132 | Double-drop: post-pass didn't recognize MoveOut-based drops | Pattern-matcher incomplete |
| 0.27.133 | Null-pointer crash: post-pass dropped zeroed moved-away param | Pattern-matcher incomplete |
| 0.27.135 | Double-drop in PEX: `has_drop` cached False before K39 finished | Cache staleness across pipeline stages |

All three are instances of the same structural problem: drop decisions made
at different times with different information, then compensated post-hoc.

### Why `_needs_drop_cache` staleness is the proximate cause

The cache poisoning timeline in the PEX path:

1. `_install_destructor_fns` → sets `destructor_fns` → cache cleared (line 270)
2. K39 initial pass → adds generic destructor entries to dict **in place**
   (`destructor_fns[inst_id] = handle.fn_id` at line 4605) — no `__setattr__`,
   **no cache clear**
3. `shared_type_table.destructor_fns = destructor_fns` at line 4661 → cache
   cleared
4. Borrow checker + trait enforcement queries `has_drop` → caches results
5. K39 post-instantiation rescan → adds more entries **in place** (line 5048)
6. `shared_type_table.destructor_fns = destructor_fns` at line 5052 → cache
   cleared
7. **More `has_drop` queries** between line 5052 and MIR lowering at 5719 →
   cache entries created from incomplete state
8. MIR lowering → reads stale cache → wrong `_param_drop_locals`

The 0.27.135 fix inserts a cache clear at step 8. But the structural fix is
to eliminate steps 7-8 as a divergence window entirely.

---

## 3. Refactor Target

### Goal

Make drop obligations explicit per-parameter per-function at MIR
construction time. Eliminate post-hoc MIR pattern scanning. Ensure `has_drop`
answers are identical regardless of source/package/PEX mode.

### Target architecture

```
                    destructor_fns finalized
                            │
                            ▼
               ┌─── cache clear ───┐
               │                   │
Borrow Checker → Drop Obligation → HIR-to-MIR → string_arc → Codegen
                     Map              │
                                      │
                                 param_drop_status
                                 (explicit on MirFunc)
```

One authoritative `has_drop` query point (after cache clear). One set of
drop facts (on MirFunc). No re-derivation. No pattern matching. No
post-pass injection.

### What replaces `__postdrop_*` inference

With stable `has_drop` (Phase A):
- `_param_drop_locals` is correct at construction time
- `_emit_scope_drops` emits drops for all params that need them
- string_arc correctly tracks `moved_out_locals` and `destructible_locals`
- The post-pass becomes an assertion, not a safety net

---

## 4. Phased Implementation Plan

### Phase A: Stabilize `has_drop` before MIR lowering [PARTIALLY DONE]

**Goal**: `has_drop` returns the same answer during MIR lowering as at the
post-pass. No stale cache entries from pre-K39 queries.

**What's done** (0.27.135): explicit `_needs_drop_cache.clear()` before the
MIR lowering loop. Regression pinned in
`test_has_drop_cache_clear_before_mir_lowering.py`.

**What remains**:
- Audit ALL `has_drop` / `is_destructible` call sites between the last
  `destructor_fns` mutation (line 5052) and MIR lowering (line 5719). Each
  call site is a potential cache-poisoning source.
- Consider: should the cache be **frozen** (read-only) after the clear, with
  any mutation raising an error? This would make the invariant enforceable
  rather than convention.
- Verify that `string_arc._type_needs_drop` agrees with `has_drop` for all
  types in the post-clear state. If it doesn't (because it uses
  `is_destructible` not `destructor_fns`), that's a latent divergence.

**Files**: `driftc.py`, `types_core.py`

**Regressions**: the three existing tests in
`test_has_drop_cache_clear_before_mir_lowering.py` cover the core contract.
Additional tests for frozen-cache enforcement if implemented.

---

### Phase B: Add `param_drop_status` to MirFunc

**Goal**: Make drop decisions explicit on the MIR function. HIR-to-MIR
lowering records what it did for each param.

**New metadata on MirFunc**:
```python
param_drop_status: Dict[str, ParamDropStatus]

class ParamDropStatus(Enum):
    DROPPED_BY_SCOPE_EXIT = auto()   # _emit_scope_drops emitted a drop
    FORWARDED_TO_CALLEE = auto()     # ownership transferred to a call (move or wrapper forward)
    MOVED_BY_EXPRESSION = auto()     # consumed by user-level move
    NO_DROP_NEEDED = auto()          # Copy type or has_drop=False
```

`FORWARDED_TO_CALLEE` covers both explicit `move` into a call and
synthesized method wrappers that forward all params by value. The semantic
meaning is the same: ownership transferred to callee, no scope-exit drop
needed. "Wrapper" is provenance, not a separate semantic category.

**Files**: `mir_nodes.py`, `hir_to_mir.py`, wrapper generation in `driftc.py`

**Regressions**:
- Assert `param_drop_status` populated for all functions
- Cross-reference against `has_drop` at post-pass: any param with
  `has_drop=True` and status `NO_DROP_NEEDED` is a Phase A gap

**Size**: Small. Additive, no behavior change.

---

### Phase C: Make string_arc consume `param_drop_status`

**Goal**: string_arc's `destructible_locals` for params derived from
`param_drop_status` rather than its own `_type_needs_drop`.

**Heuristics removed**:
- `string_arc._type_needs_drop` for params
- The per-param portion of `destructible_locals` computation

**Key risk**: string_arc handles non-param locals too. Only change the
param path initially.

**Files**: `string_arc.py`

**Regressions**: test where `_type_needs_drop` disagrees with `has_drop` —
`param_drop_status` is authoritative.

---

### Phase D: Post-pass becomes assertion only

**Goal**: Replace all injection logic with: for each param where
`has_drop=True`, assert `param_drop_status` is not `NO_DROP_NEEDED`.

**Heuristics removed**:
- Entire MIR pattern-scanning loop
- Zero-store move-away detection
- `__postdrop_*` injection code

**Files**: `driftc.py`

**Regressions**: all existing tests pass with injection removed.

**Prerequisites**: A + B + C validated with downstream (drift-web, net.tls).

---

### Phase E: Unify `_type_needs_drop` functions (medium-term)

**Goal**: One authoritative `has_drop` function, called at one point, result
recorded as fact. Remove:
- `hir_to_mir._needs_runtime_drop` (replace with `has_drop`)
- `string_arc._type_needs_drop` (replace with `param_drop_status` for params,
  `has_drop` for locals)

This eliminates the three-function divergence table entirely.

**Files**: `hir_to_mir.py`, `string_arc.py`, `types_core.py`

**Key risk**: `_needs_runtime_drop` also checks `_contains_dv_transitive`
(DiagnosticValue containment). Must verify `has_drop` covers this case.

---

### Phase F: Thread borrow checker facts (long-term)

**Goal**: Consume borrow checker's `PlaceState` dataflow results instead of
HIR-to-MIR re-deriving ownership. Prerequisite: Phase A (stable `has_drop`).

**Files**: `borrow_checker_pass.py`, `driftc.py`, `hir_to_mir.py`

---

## 5. Non-Goals

### No more growing the pattern matcher

The post-pass pattern scanner is not extensible. Every new MIR shape
requires a new detection rule. Eliminate the need for pattern matching, not
add more patterns.

### No package/dependency-order sensitivity

Drop decisions must not change based on which packages are loaded. `has_drop`
must return the same answer regardless of `--dep net-tls`. This is Phase A.

### No separate drop-checking functions with separate caches

One `has_drop`, called once per type per compilation, result cached reliably.
Not three functions with three caches making three different decisions.

### No relying on MIR instruction shapes as semantic source of truth

`LoadLocal + ZeroValue + StoreLocal` is a MIR encoding of "move", not a
semantic fact. Ownership state should be stated explicitly on MirFunc, not
reverse-engineered from instruction patterns.

### No mode-conditional drop behavior

"Source path passes, PEX path fails" is the definition of mode divergence.
The fix is not "add another special case for PEX" — it's "make the pipeline
mode-independent after artifact ingress."

---

## 6. Relationship to Semantic-Ingestion Refactor

Both plans are **anti-mode-divergence plans**:

| Subsystem | Source of divergence | Ingestion fix | Drop fix |
|-----------|---------------------|--------------|---------|
| Type identity | Order-dependent nominal declaration | Pre-declare all nominals | N/A |
| `has_drop` stability | Cache poisoned between K39 and MIR lowering | N/A | Cache clear + freeze |
| Drop decisions | Three independent `_type_needs_drop` functions | N/A | Single authoritative `has_drop` |
| Post-hoc sweeps | `_coerce_forward_nominal` + `__postdrop_*` | Assertion-only | Assertion-only |

Both converge on: **facts established once before any consumer, no later
pass re-derives or compensates. Mode affects inputs, not meaning.**

---

## Summary

| Phase | Goal | Removes | Status |
|-------|------|---------|--------|
| **A** | Stabilize `has_drop` before MIR lowering | Cache staleness | **Partially done** (0.27.135) |
| **B** | Add `param_drop_status` to MirFunc | Nothing (additive) | Proposed |
| **C** | string_arc consumes status for params | `_type_needs_drop` for params | Proposed |
| **D** | Post-pass becomes assertion | All pattern-matching + injection | Proposed |
| **E** | Unify `_type_needs_drop` functions | Three-function divergence | Medium-term |
| **F** | Thread borrow checker facts | HIR-to-MIR re-derivation | Long-term |

A is partially done. B is the next high-value step (small, additive, enables
C and D). D is the payoff — deleting the entire `__postdrop_*` mechanism.
