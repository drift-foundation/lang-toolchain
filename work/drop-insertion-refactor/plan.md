# Drop Insertion / Ownership Refactor Plan

## Status: proposal (review before execution)

---

## 1. Current Drop Pipeline End-to-End

### Stage 1: Type Checker + Borrow Checker (HIR level)

The borrow checker (`borrow_checker_pass.py:134`) runs on typed HIR before MIR
lowering. It computes per-place ownership state via dataflow:

```python
class PlaceState(Enum):
    UNINIT = auto()   # uninitialized
    VALID = auto()    # holds an owned value
    MOVED = auto()    # ownership transferred
```

It produces `PlaceState` per-place per-CFG-node, tracks moves, detects
use-after-move, and validates borrow lifetimes. It runs at `driftc.py:5438`
(inside `compile_stubbed_funcs`).

**Problem**: its output is used for diagnostics only (`borrow_diags`). The
ownership facts are not threaded to MIR lowering. They are discarded after
checking.

### Stage 2: HIR-to-MIR Lowering (`hir_to_mir.py`)

The HIR-to-MIR lowerer independently re-derives ownership state:

- **`_param_drop_locals`** (line 303): populated at construction time by
  checking `_needs_runtime_drop(ty)` and `_type_is_destructible(ty)` for each
  param. This determines which params get scope-exit drops.
- **`_moved_locals`** (set): tracks locals consumed by `move` expressions.
  `_emit_scope_drops` skips moved locals.
- **`_emit_scope_drops`** (line 596): called before each Return/break/continue
  terminator. Emits `MoveOut + DropValue` for each in-scope local that needs
  drop and hasn't been moved.
- **`_scope_stack`**: LIFO stack of locals per lexical scope.

The ownership/drop decisions here depend on `has_drop` / `is_destructible` at
the time the lowerer runs. These queries depend on `destructor_fns` being
installed — which may not be complete yet.

### Stage 2.5: string_arc Pass (`string_arc.py`)

`insert_string_arc` is a MIR-to-MIR pass (line 22) that:

1. Expands `MoveOut` to `LoadLocal + ZeroValue + StoreLocal` (line 807-819)
2. Independently re-derives which locals need drop using its own
   `_type_needs_drop` function (line 63-108) with its **own cache**
3. Tracks `moved_out_locals` per-block via dataflow (line 604-633)
4. Emits `_drop_all_destructibles` at return blocks (line 1242) for locals in
   `destructible_locals` not in `skip_cleanup_locals`

**Problem**: `_type_needs_drop` uses `is_destructible()` (trait prover) but
does NOT check `destructor_fns` at the field recursion level. It can disagree
with `type_table.has_drop()`. It also has its own cache that is never
invalidated.

### Stage 3: Post-Pass Drop Injection (`driftc.py`)

`_postdrop_inject_missing_param_drops` (line 3019) runs after MIR lowering
and string_arc. It:

1. Clears `_needs_drop_cache` (line 7085) — ensures fresh `has_drop` answers
2. For each param where `has_drop` is True, scans the MIR for:
   - Existing `LoadLocal/MoveOut → DropValue` chains
   - The `LoadLocal + ZeroValue + StoreLocal` move-away pattern
3. If no existing drop or move-away is found, injects `__postdrop_*` sequences
   into every Return-terminating block

**Problem**: this is the pattern-matcher that keeps breaking. It infers
ownership state by scanning MIR instruction shapes instead of consuming
explicit facts. Every new MIR shape (MoveOut, LoadLocal, zero-store moves,
multi-block control flow) requires a new detection rule.

### Stage 4: LLVM Codegen (`llvm_codegen.py`)

`_emit_drop_value` (line 8773) dispatches based on:
- `destructor_fns[ty_id]` → call destroy function
- No destructor → field-by-field `extractvalue` + recursive drop
- ZeroValue structs get field-dropped (null Arc pointers → crash if runtime
  doesn't guard null)

No ownership metadata is consumed — it trusts that every `DropValue`
instruction is correct.

---

## 2. The Architectural Seam

### The core problem

Ownership state is determined **three times independently**, each with
different logic and different inputs:

| Stage | Function | Uses `destructor_fns`? | Uses `is_destructible`? | Has own cache? |
|-------|----------|----------------------|------------------------|----------------|
| HIR-to-MIR | `_needs_runtime_drop` / `_type_is_destructible` | via `has_drop` | Yes | TypeTable cache |
| string_arc | `_type_needs_drop` | **No** (only `is_destructible`) | Yes | **Own** cache |
| Post-pass | `has_drop` | Yes | Yes | TypeTable cache (cleared) |

When these three disagree, bugs result:
- **0.27.132 bug**: HIR-to-MIR uses `MoveOut`, post-pass only recognized
  `LoadLocal` → missed existing drop → double-drop
- **0.27.133 bug**: string_arc moved param away (zero-stored), but post-pass
  didn't recognize the move pattern → injected drop of zeroed value → null
  Arc crash

### Why pattern-matching keeps failing

The post-pass must reverse-engineer ownership state from MIR instructions
because no explicit state is propagated. Each new MIR shape that the lowerer
or string_arc produces requires a corresponding detection rule in the
post-pass. The set of shapes is open-ended:

- `MoveOut + DropValue` (scope-exit drop)
- `LoadLocal + DropValue` (string_arc destructible cleanup)
- `LoadLocal + ZeroValue + StoreLocal` (move into call)
- `LoadLocal + ZeroValue + StoreLocal` in block A, `Return` in block B
  (cross-block move)
- Future: conditional moves, partial moves, catch-arm ownership changes

### Why `destructor_fns` timing matters

`destructor_fns` is populated incrementally:
1. Pre-install via `_scan_destructible_impls_by_name` (line 3649)
2. K39 augments with generic instantiation entries (line 4605, 4607)
3. K39 post-instantiation rescan adds more (line 4996)

All three run before MIR lowering. But `has_drop` results depend on which
entries exist. If a struct's transitive drop status depends on a generic
destructor (e.g., `Arc<T>::destroy`) that K39 registers for a specific
instantiation, `has_drop` can return different values at different pipeline
stages. The `_needs_drop_cache` compounds this — stale entries persist across
K39 rounds.

---

## 3. Refactor Target

### Goal

Make drop obligations explicit per-parameter per-function at MIR construction
time. Eliminate post-hoc MIR pattern scanning for drop injection.

### Target architecture

```
Borrow Checker (HIR)  ──►  Drop Obligation Map  ──►  HIR-to-MIR  ──►  string_arc  ──►  Codegen
        ▲                         │
        │                    explicit facts:
   PlaceState per           - param needs_drop: bool
   place per node           - param moved_away: bool
                            - param drop_emitted: bool
```

A **`DropObligationMap`** would be a per-function data structure recording:
- For each parameter: whether it needs a scope-exit drop at function exit
- For each local: whether it was moved away (ownership transferred) and if so,
  in which block(s)
- For each return path: which params/locals still need drops

This map would be:
1. **Produced** by the borrow checker (which already computes these facts) or
   by a dedicated drop-planning pass that runs after type checking and
   `destructor_fns` finalization
2. **Consumed** by HIR-to-MIR lowering to emit the correct drops
3. **Verified** by string_arc (which should trust the map rather than
   re-deriving from scratch)
4. **Not re-derived** by any post-pass

### What replaces `__postdrop_*` inference

The post-pass exists because `has_drop` can return False during MIR lowering
and True afterward. The fix is to ensure `has_drop` is stable before MIR
lowering starts — not to patch up afterward.

With stable `has_drop`:
- `_param_drop_locals` is correct at construction time
- `_emit_scope_drops` emits drops for all params that need them
- string_arc correctly tracks `moved_out_locals` and `destructible_locals`
- No post-pass needed

### How ownership state should be represented

Instead of inferring "was this param dropped?" from MIR instruction shapes,
MirFunc should carry explicit metadata:

```python
@dataclass
class MirFunc:
    # ... existing fields ...
    # NEW: per-param drop status computed during lowering
    param_drop_status: Dict[str, ParamDropStatus] = field(default_factory=dict)

class ParamDropStatus(Enum):
    DROPPED_BY_SCOPE_EXIT = auto()   # _emit_scope_drops emitted a drop
    MOVED_TO_CALLEE = auto()         # ownership transferred to a call
    NO_DROP_NEEDED = auto()          # type doesn't need drop (Copy/scalar)
```

string_arc would read and update this metadata rather than re-deriving it.
The post-pass (if retained at all) would be a simple assertion: "every param
with `has_drop=True` has a non-`NO_DROP_NEEDED` status."

---

## 4. Phased Implementation Plan

### Phase A: Stabilize `has_drop` before MIR lowering

**Goal**: Ensure `destructor_fns` is finalized and `_needs_drop_cache` is
cleared exactly once, before any HIR-to-MIR lowering begins. Eliminate the
root cause of `has_drop` returning different values at different pipeline
stages.

**Files/subsystems**:
- `driftc.py`: audit the timeline of `destructor_fns` installation relative
  to MIR lowering. Ensure K39 finishes before the first `HIRToMIR()` call.
  Add an explicit cache clear after the last `destructor_fns` mutation.
- `driftc.py`: when `pass1_state` is provided, verify the driver's pre-install
  at line 10688 captures ALL destructors (including K39 generic instantiations)

**New facts/metadata**: none yet — this is a prerequisite fix.

**Heuristics removed**: none yet.

**Expected regressions/tests**:
- Assert that `destructor_fns` dict identity doesn't change between MIR
  lowering start and post-pass
- Pin `has_drop` stability for cross-package generic types (e.g.,
  `Arc<AtomicBool>`) across the lowering→postpass boundary

**Key risk**: K39 generic instantiation creates new struct instances and new
destructor entries during `_drain_instantiations`, which interleaves with MIR
lowering of instantiated functions. May need to separate "discover all types"
from "lower all MIR".

---

### Phase B: Add `param_drop_status` to MirFunc

**Goal**: Make drop decisions explicit on the MIR function. HIR-to-MIR
lowering records what it did (or decided not to do) for each param.

**Files/subsystems**:
- `mir_nodes.py`: add `param_drop_status: Dict[str, str]` to `MirFunc`
- `hir_to_mir.py`: after lowering, record which params were added to
  `_param_drop_locals` and which were in `_moved_locals` at function exit

**New facts/metadata**:
```python
# On MirFunc, after lowering:
param_drop_status = {
    "handle": "scope_exit_drop",   # _emit_scope_drops emitted MoveOut+DropValue
    "count": "no_drop",            # Int, doesn't need drop
    "callback": "moved",           # consumed by move expression
}
```

**Heuristics removed**: none yet — this phase adds data, doesn't remove logic.

**Expected regressions/tests**:
- Assert `param_drop_status` is populated for all functions
- Cross-reference `param_drop_status` against `has_drop` at post-pass time:
  any param with `has_drop=True` and status `"no_drop"` is a potential
  `has_drop` instability (Phase A gap)

**Key risk**: minimal — additive metadata, no behavior change.

---

### Phase C: Make string_arc consume `param_drop_status`

**Goal**: string_arc's `destructible_locals` and `moved_out_locals` for params
should be derived from `param_drop_status` rather than re-querying the type
table with its own `_type_needs_drop` function.

**Files/subsystems**:
- `string_arc.py`: for params, consult `func.param_drop_status` instead of
  `_is_destructible_tid(local_types.get(name))`
- `string_arc.py`: if a param has status `"scope_exit_drop"`, it's already in
  `destructible_locals` equivalent — don't re-derive
- `string_arc.py`: if a param has status `"moved"`, pre-seed
  `moved_out_locals` — don't rely on seeing MoveOut instructions

**New facts/metadata**: consumed, not produced.

**Heuristics removed**:
- string_arc's `_type_needs_drop` for params (still needed for non-param
  locals until those are tracked too)
- The per-param portion of `destructible_locals` computation

**Expected regressions/tests**:
- All existing drop tests must pass
- New test: param where `_type_needs_drop` disagrees with `has_drop` — the
  `param_drop_status` should be authoritative

**Key risk**: string_arc is complex and handles non-param locals too. Must be
careful to only change the param path.

---

### Phase D: Narrow the post-pass to an assertion

**Goal**: With stable `has_drop` (Phase A) and explicit `param_drop_status`
(Phase B), the post-pass should have nothing to inject. Replace injection
logic with an assertion.

**Files/subsystems**:
- `driftc.py`: `_postdrop_inject_missing_param_drops` → replace the injection
  loop with: for each param where `has_drop=True`, assert that
  `param_drop_status` is not `"no_drop"`. If it is, that's a pipeline error
  (Phase A didn't stabilize `has_drop`), not something to silently fix.

**Heuristics removed**:
- The entire MIR pattern-scanning loop (LoadLocal/MoveOut → DropValue scan)
- The zero-store move-away detection
- The `__postdrop_*` injection code

**Expected regressions/tests**:
- All existing tests pass with injection removed (the assertion doesn't fire)
- If any test fires the assertion, it reveals a Phase A gap

**Key risk**: this is the "prove it works" phase. Must be validated with
downstream projects (drift-web, net.tls) before merging.

---

### Phase E: Extend to non-param locals (optional, future)

**Goal**: Extend `param_drop_status` to a per-local `drop_status` covering
all locals, not just params. This would let string_arc fully consume explicit
facts rather than re-deriving ownership.

**Files/subsystems**:
- `hir_to_mir.py`: track drop status for all locals in `_scope_stack`
- `mir_nodes.py`: `local_drop_status: Dict[str, str]`
- `string_arc.py`: consume `local_drop_status` for all locals

**Key risk**: significantly larger surface area. Params are the high-value
target; locals are a longer-term cleanup.

---

### Phase F: Thread borrow checker facts to MIR lowering (optional, future)

**Goal**: Instead of HIR-to-MIR re-deriving ownership state, consume the
borrow checker's `PlaceState` dataflow results. The borrow checker already
computes UNINIT/VALID/MOVED per-place per-CFG-node — this is strictly more
information than `_param_drop_locals` + `_moved_locals`.

**Files/subsystems**:
- `borrow_checker_pass.py`: export per-function `PlaceState` summaries
  (not just diagnostics)
- `driftc.py`: pass borrow checker output to `HIRToMIR` constructor
- `hir_to_mir.py`: use borrow checker facts instead of local re-derivation

**Key risk**: the borrow checker operates on HIR places (binding IDs), while
MIR operates on local names. The mapping is straightforward but must be
maintained. Also, the borrow checker runs before `destructor_fns` finalization,
so its drop-needs queries may be wrong — Phase A is a hard prerequisite.

---

## 5. Specific Questions Answered

### What would replace the current `__postdrop_*` inference logic?

Phase B's `param_drop_status` makes the drop decision explicit at lowering
time. The post-pass becomes an assertion rather than inference + injection.
If `has_drop` is stable (Phase A), the lowerer's decisions are final.

### How should "moved away", "consumed by callee", "needs drop on return" be represented?

As explicit enum values on `MirFunc.param_drop_status`:
- `SCOPE_EXIT_DROP`: lowerer emitted `MoveOut + DropValue` (or string_arc
  expanded it to `LoadLocal + ZeroValue + StoreLocal + DropValue`)
- `MOVED_TO_CALLEE`: value was forwarded to a call and the local was zeroed
- `MOVED_BY_EXPRESSION`: consumed by a `move` expression in user code
- `NO_DROP_NEEDED`: type is Copy or has_drop is False

### Can the post-pass be deleted entirely, or only narrowed?

**Deleted entirely** after Phases A-D. The post-pass exists because `has_drop`
was unreliable during MIR lowering. With stable `has_drop`, every param gets
the correct drop during lowering. The post-pass would be a zero-injection
assertion only, and could be compiled out in release builds.

### What prerequisites exist before we can trust earlier ownership facts?

1. `destructor_fns` must be complete before MIR lowering (Phase A)
2. `_needs_drop_cache` must be cleared after the last `destructor_fns` mutation
3. K39 generic instantiation must not interleave with MIR lowering of
   non-instantiated functions
4. string_arc's `_type_needs_drop` must agree with `has_drop` — or be removed
   in favor of consuming `param_drop_status` (Phase C)

---

## 6. Non-Goals / Things to Avoid

### No more growing the pattern matcher

The post-pass pattern scanner (`LoadLocal/MoveOut → DropValue`, zero-store
move detection, reinit check) is not extensible. Each new MIR shape requires
a new rule. The refactor goal is to eliminate the need for pattern matching,
not to add more patterns.

### No package/dependency-order sensitivity

Drop decisions must not change based on which packages are loaded or in what
order. `has_drop` must return the same answer regardless of whether
`--dep net-tls` is present. This is the Phase A prerequisite.

### No relying on incidental MIR shapes as semantic source of truth

`LoadLocal + ZeroValue + StoreLocal` is a MIR encoding of "move", not a
semantic fact. string_arc produces this encoding from `MoveOut`, which itself
is a MIR encoding. The semantic fact is "this param's ownership was
transferred to this call" — that should be stated explicitly, not reverse-
engineered from instruction patterns.

### No separate drop-checking functions with separate caches

Today there are three implementations of "does this type need drop":
- `TypeTable.has_drop` (checks `destructor_fns`, `is_destructible`, fields)
- `hir_to_mir._needs_runtime_drop` (checks `has_drop` + `_contains_dv_transitive`)
- `string_arc._type_needs_drop` (checks `is_destructible`, fields — NO `destructor_fns`)

There should be one authoritative function (`has_drop`), called at one point
in the pipeline (after `destructor_fns` is finalized), with its result
recorded as a fact.

---

## Summary

| Phase | Goal | Removes | Prerequisites | Size |
|-------|------|---------|---------------|------|
| **A** | Stabilize `has_drop` before MIR lowering | Cache staleness bugs | None | Medium |
| **B** | Add `param_drop_status` to MirFunc | Nothing (additive) | None | Small |
| **C** | string_arc consumes `param_drop_status` for params | `_type_needs_drop` for params | B | Medium |
| **D** | Post-pass becomes assertion only | All pattern-matching + injection code | A + B + C | Small |
| **E** | Extend to non-param locals | `_type_needs_drop` for all locals | D | Large |
| **F** | Thread borrow checker facts | HIR-to-MIR re-derivation | A + E | Large |

Phases A-D are the immediate refactor. E and F are future cleanup.
A and B are independent and can proceed in parallel. C requires B.
D requires A + B + C and downstream validation.
