# Ownership Ledger — Phase 3A Design

Branch: `feature/ownership-ledger-rollout`
Compiler baseline: 0.31.7 (certified 2026-04-22)
Status: design — no code yet

## Goal

Build a shared `LiveStateMap` for per-local ownership state at every MIR program point, run it **observationally alongside the existing passes** for a full CI cycle, and use the ledger-vs-existing disagreement stream as the gate for Phase 3B (consumer-at-a-time swap-over). No emitted MIR changes in 3A.

This note is the decision record for 3A. It defines the lattice, the builder contract, the instrumentation surface, and the gate criteria for moving to 3B.

## Non-goals for 3A

- **Do not change emitted MIR.** Every existing site emits exactly what it emits today. The ledger is a reader that logs.
- **Do not handle conditional-move semantics.** if-let / partial-bind path-sensitive moves are `MaybeUninit` at the join and flagged; the decision between runtime drop flags and CFG splits is 3C (separate design note, must land before 3B).
- **Do not retire `DropPolicy`.** The ledger layers *on top of* `DropPolicy` — policy answers "does this *type* need a drop"; the ledger answers "does this *local at this program point* own a value to drop." Orthogonal axes.
- **Do not touch the three-quadrant pin** at `lang/tests/stage2/test_match_scrut_copy_store_emits_copyvalue.py`. This is the Phase 2a invariant set; if the ledger disagrees with it, the ledger is wrong.

## Current emission sites (map from survey)

| # | File | Site | Owns state | Predicate |
|---|---|---|---|---|
| 1 | `hir_to_mir.py:885` `_emit_scope_drops` | scope-drop on block/fn exit | `_moved_locals: set[str]`, `_scope_stack`, `_local_types` | emit Drop iff local ∉ moved AND (`needs_drop` OR `is_destructible`) |
| 2 | `hir_to_mir.py:1258–1610` match-arm cleanup | partial-move after match binder moves | `arm_scrut_payload_moved: bool`, `moved_field_indices: set[int]`, `arm_drop_locals` | per-field: emit Drop iff field ∉ moved AND field needs drop |
| 3 | `string_arc.py:170` `destructible_locals` + `_drop_all_destructibles` | string-arc return-path cleanup | `destructible_locals: Set[str]`, `initialized_destructibles: Set[str]`, `nullsafe_destructible_locals` | emit Drop iff local ∈ destructibles AND (initialized OR nullsafe) |
| 4 | `string_arc.py:828` drop-before-overwrite | Drop before StoreLocal to destructible | `initialized_destructibles` (CFG `assigned_in` dataflow) | emit Drop iff StoreLocal target ∈ destructibles AND ∈ initialized |

Cross-site coupling: Sites 1 ↔ 2 share `_moved_locals`; Sites 3 ↔ 4 share `initialized_destructibles`. Sites 1, 2 run *during* HIR→MIR (state mutated in flight). Sites 3, 4 run in the `string_arc` pass *after* HIR→MIR completes.

## LiveStateMap — data model

```
LiveStateMap: dict[ProgramPoint, LocalStateView]
ProgramPoint = (block_name: str, instr_idx: int)    # post-instruction state
LocalStateView: dict[local_name: str, LiveState]
```

**State lattice — raw provenance** (what the bits look like, not what drop should do):

```
Uninit        — declared, never assigned on any predecessor path
Live          — owns its value; drop must run at end of scope / on overwrite
MovedOut      — logical ownership has been transferred; drop must NOT run
Tombstoned    — zero-bytes written; runtime drop is a safe no-op
MaybeUninit   — JOIN of predecessors that disagree on a drop-relevant axis
```

These five are distinct because they have distinct *provenance* (different histories, different debug/diagnostic meanings), not because they all matter to the drop decision. See "Drop-decision classifier" below for how the ledger collapses them into a verdict.

**Post-instruction** (not pre). Pre-state is reconstructible by looking at the previous instruction's post-state or the block's in-state for `instr_idx == 0`. This matches Rust MIR convention and makes the builder straightforward: each instruction has a transfer function applied to its pre-state.

**Transfer functions** (MIR op → state effect on each local it touches):

| MIR op | Effect |
|---|---|
| `StoreLocal(local, _)` | local → Live (unconditional; see refinement rule below) |
| `MoveOut(_, local, _)` | local → MovedOut |
| `ZeroValue` + `StoreLocal(local, zero)` | local → Tombstoned |
| `DropValue(tmp)` | tmp → (consumed, falls off local set) |
| scope-exit | drop-decision classifier consulted per local; then local removed |
| fn entry | every param → Live; every declared local → Uninit |

**Refinement rule (MaybeUninit is absorbing at joins only).** `MaybeUninit` is the join result when predecessors disagree on a drop-relevant axis (see classifier below). It is **not** a sink state for the whole lifetime: a later `StoreLocal(local, _)` definitively writes the local and brings the state back to `Live`. Same for `MoveOut(_, local, _)` → `MovedOut`, and zero-store → `Tombstoned`. In other words: `MaybeUninit` tells you "at this point, state is path-dependent" — a subsequent deterministic transfer can unshadow that uncertainty. Without this rule the ledger would over-report 3C cases: a typical `let x; if cond { x = a } else { x = b }; use(x)` pattern would permanently carry the post-join `MaybeUninit` even though `x` is unambiguously `Live` after the join's later definite write.

**Block-join merge rule:**

```
join(s, s)                 = s                        (any state with itself)
join(Live, X)              = MaybeUninit              where X ∈ {Uninit, MovedOut, Tombstoned, MaybeUninit}
join(non-Live-drop, X)     = drop-equivalent join     (see below — MovedOut ∪ Tombstoned ≠ MaybeUninit)
```

Here's the key refinement: because `MovedOut`, `Tombstoned`, and `Uninit` are all **non-owning** for the drop decision, joining any two of them does not produce `MaybeUninit` — it produces the most conservative representative among them that preserves their shared "do-not-drop" verdict. Specifically:

```
join(MovedOut, Tombstoned) = MovedOut      (both non-owning; MovedOut is conventional)
join(MovedOut, Uninit)     = MovedOut
join(Tombstoned, Uninit)   = Tombstoned
```

Only `Live ∪ {anything-else}` and `MaybeUninit ∪ {anything-not-equal}` produce `MaybeUninit`. This keeps the 3C queue small: a path that moves on one arm and tombstones on the other is not a conditional-drop case, because both arms agree on "no drop." A path that is `Live` on one arm and `Uninit` on the other *is* a conditional-drop case — that's the real 3C material.

## Drop-decision classifier

The ledger exposes a derived view separate from raw state:

```
DropVerdict ∈ {MustDrop, MustNotDrop, PathDependent}
```

Mapping:

| Raw state | Verdict |
|---|---|
| `Live` | `MustDrop` (modulo `DropPolicy.needs_drop` on the type — a `Live` POD still requires no drop) |
| `MovedOut` | `MustNotDrop` |
| `Tombstoned` | `MustNotDrop` (the bytes are zero; runtime drop would be a no-op but explicitly suppressing is cleaner) |
| `Uninit` | `MustNotDrop` |
| `MaybeUninit` | `PathDependent` — the 3C signal |

Sites ask the ledger for a `DropVerdict`, not a raw state. Disagreement-reporter comparisons are verdict-level: `MustDrop` vs `MustNotDrop` vs `PathDependent`. This is what K's third point requires — `MovedOut` and `Tombstoned` are distinct in the provenance layer (useful for diagnostics, future Phase 4 fusion) but collapse to the same `MustNotDrop` verdict at the drop decision. Joining them cannot produce a false `PathDependent`.

## Builder architecture

**Input:** complete MIR for one function, post-HIR→MIR, pre-`string_arc`.
**Output:** `LiveStateMap` for that function.
**Location:** new module `lang/driftc/stage2/ownership_ledger.py`.

```
def build_ledger(func: M.MirFunction, *, drop_policy: Callable[[TypeId], DropPolicy]) -> LiveStateMap: ...
```

Algorithm: worklist dataflow over the CFG.
- Block in-state = join of predecessor out-states (using merge rule above).
- Apply each instruction's transfer function sequentially within the block.
- Block out-state = post-state of last instruction.
- Fixed point is guaranteed because the lattice is finite and transfer functions are monotone.

The builder consults `DropPolicy` via a passed-in callable so it does not bypass the funnel (Phase 1 contract). It does **not** call `TypeTable` directly. This is important: the ledger must respect the existing policy surface or its disagreements are uninterpretable.

**What the builder does NOT know about in 3A:**
- `__match_field_move_X` indirection — the ledger sees the underlying MoveOut and tracks the field local; the site-level flag is opaque.
- Site 3's nullsafe override — the ledger reports state; whether drop is emitted is a *site* question. For 3A that's fine because the site still decides.

## Disagreement reporter

The ledger is instantiated lazily when `DRIFT_OWNERSHIP_LEDGER=observe` is set in the environment. Off otherwise — zero cost for normal builds.

**Per-site instrumentation contract:**

Each of the 4 sites, at the exact point it decides "emit drop / skip drop / emit copy / transfer ownership," calls:

```python
ledger.check(site="scope_drop" | "match_cleanup" | "string_arc_return" | "drop_before_overwrite",
             point=(block_name, instr_idx), local=name,
             site_verdict=<drop|skip|copy|move>,
             site_reason=<short string>)
```

The ledger compares `site_verdict` against what the ledger's state at `point[local]` says should happen, classifies the result as:

- `agree` — silent
- `disagree_ledger_stricter` — ledger says no drop, site emits drop (potential double-drop avoided by site; likely a *ledger-incompleteness* bug)
- `disagree_site_stricter` — site says no drop, ledger says drop (potential leak avoided by site; likely also *ledger-incompleteness*)
- `disagree_semantically_equivalent` — e.g., site drops a Tombstoned local (safe no-op); ledger would skip. Both correct, different shapes.

Disagreements emit a structured JSON record to stderr keyed by site, point, local, kind, verdict, reason, and a short stack fingerprint. A separate helper script aggregates records across a CI run into a triage table.

**No exceptions, no aborts in 3A.** The reporter is pure telemetry.

## Sites 1 and 2 — decision-event capture (revised)

Sites 1 and 2 run *during* HIR→MIR lowering, so the ledger cannot be built yet (the MIR is not complete). The earlier draft proposed pure retrospective inference: build the ledger on the finished MIR and match `MoveOut+DropValue` shapes to expected decisions. **That plan is insufficient and has been revised.**

The failure mode: if site 1 decides "skip drop" and emits nothing, the finished MIR carries no evidence that a decision point ever existed at that program point. A retrospective ledger that sees no `DropValue` cannot tell the difference between "site correctly skipped" and "site incorrectly skipped (= leak)." The leak case is exactly what we want 3A to catch, so retrospective alone fails the observational contract.

**Revised approach: explicit decision-event log + retrospective ledger.**

Sites 1 and 2 emit a structured **decision event** at the point they make their verdict, independent of whether they emit MIR. The event is written to an in-memory log on `HIRToMIR` and drained by the reporter once the ledger is built.

```python
# new: lang/driftc/stage2/ownership_ledger_events.py
@dataclass(frozen=True, slots=True)
class DropDecisionEvent:
    site: str                       # "scope_drop" | "match_cleanup"
    fn_name: str
    program_point: tuple[str, int]  # (block_name, instr_idx_where_drop_would_land)
    local: str
    verdict: str                    # "MustDrop" | "MustNotDrop"
    reason: str                     # short tag: "moved", "no-drop-policy", "destructible", ...
```

Call sites for site 1 (`_emit_scope_drops`, `hir_to_mir.py:885`):

- Before `continue` on `local in self._moved_locals` (line 892) → emit `DropDecisionEvent(verdict="MustNotDrop", reason="moved")`
- Before `continue` on no-drop-policy (line 897) → emit `DropDecisionEvent(verdict="MustNotDrop", reason="policy")`
- Before `self.b.emit(M.MoveOut ...)` (line 900) → emit `DropDecisionEvent(verdict="MustDrop", reason="needs_drop" | "destructible")`

Same shape for site 2 (`hir_to_mir.py:1556–1610`): every branch of the per-field cleanup loop emits an event before either skipping or emitting the `VariantGetField + MoveOut + DropValue` sequence.

**Pipeline:**

```
HIR → MIR                   (during lowering, sites 1/2 record DropDecisionEvents)
      ↓
MIR complete                 
      ↓
build_ledger(mir)           (post-lowering, pre-string_arc)
      ↓
reporter.compare_events(    (events recorded above vs ledger verdict at same program point)
    events, ledger)
      ↓
string_arc pass              (sites 3/4 use prospective hook against the now-built ledger)
```

Because events carry their own `program_point`, the reporter does not need to infer site-1/2 decision points from MIR shape. The ledger is consulted at exactly the recorded point and produces its own verdict; the comparison is then 1:1 with the site's verdict. Leak cases ("site skipped but ledger says MustDrop") are now detectable.

The event log is free when the ledger is disabled: the recording call sites check `debug.enabled("ownership_ledger")` once per function and short-circuit. Zero per-decision overhead when off.

Sites 3 and 4 stay as prospective hooks — they're in a post-MIR pass, so the ledger is already built and they can call `ledger.verdict_at(point, local)` directly at decision time.

## Env-flag gating

The ledger is compiler-internal dev telemetry and belongs in the existing compiler-debug namespace at `lang/driftc/debug.py` (which already accepts a JSON object under `DRIFT_COMPILER_DEBUG`). No new top-level `DRIFT_*` var.

Primary form — JSON object:

```
DRIFT_COMPILER_DEBUG='{"ownership_ledger": true}'
```

Gate in code:

```python
from lang.driftc import debug
if debug.enabled("ownership_ledger"):
    ...
```

This piggybacks on the existing flag machinery alongside `convergence_parity`, `pkg_hir`, `e2e_diags`, etc. No changes to `debug.py` are required for 3A's single-mode observational pass — `enabled()` already returns a bool.

For 3B/3C future modes (`enforce`, `rewrite`), the cleanest extension is separate bool keys (`ownership_ledger_enforce`, `ownership_ledger_rewrite`) rather than widening `debug.py` to accept string values. Decide when we get there — 3A doesn't constrain it.

A shorthand CI convenience var is **not** introduced in 3A. If teams need it later we can add an alias in `debug.py`, but one namespace is cleaner while the feature is dev-only.

## Gate criteria for 3A → 3B

3A is **done** and 3B may begin when **all** of:

1. Full `PYTHONPATH=. pytest lang/tests/ -n16` (unfiltered, per feedback) passes with `DRIFT_COMPILER_DEBUG='{"ownership_ledger":true}'` set — i.e., the ledger builds on every function in the test suite without crashing.
2. The three-quadrant pin (`test_match_scrut_copy_store_emits_copyvalue.py`) has **zero** verdict disagreements (`MustDrop` vs `MustNotDrop`). Verdict comparisons are at the `DropVerdict` layer, not raw state, so `MovedOut`/`Tombstoned` provenance differences that still agree on `MustNotDrop` do not count as disagreements.
3. The full e2e suite (`PYTHONPATH=. pytest lang/tests/codegen/e2e/ -n16`) with the flag on produces a disagreement report where **every disagreement has been triaged** into one of:
   - (a) ledger-is-right-site-is-wrong (site verdict differs from ledger verdict, and the ledger is correct) → 3B will fix when that consumer is swapped
   - (b) ledger-is-wrong-site-is-right → 3A ledger bug, must fix before 3B starts
   - (c) `PathDependent` verdict at a drop site → held for 3C

   Category (b) must be empty. Category (a) is the point of the exercise. Category (c) is the input to 3C's design note.
4. The 3C design note (runtime-drop-flags vs CFG-split) is written and reviewed. 3B consumer swaps cannot start without it because the first consumer swap may expose a (c) case that only 3C can resolve.

## What 3A explicitly does not touch

- `DropPolicy` (the five-axis funnel) — untouched. The ledger layers on top.
- Phase 1 residuals — the ledger may make them reachable via new axes in 3B, but 3A does not rewrite any of them.
- `arm_scrut_payload_moved`, `__match_field_move_X`, `_moved_locals`, `destructible_locals`, `initialized_destructibles` — all still live, still author their own state. The ledger reads, does not write.
- MIR-level ownership changes — no new MIR ops in 3A. `TombstoneValue` as a first-class op is Phase 4.

## Open questions (not blocking 3A)

1. Should the ledger track temps (SSA values) or only named locals? Named-only is simpler and matches the survey's finding (`destructible_locals` excludes `__` internals). Proposed: named-only for 3A, revisit if site 2's field-move indices need temp-level tracking.
2. Multi-function interprocedural state (e.g., a local passed to a function that conditionally moves)? Out of scope — ownership transfer at call boundaries is already modeled in `DropPolicy.is_cheap_copy` + the emitted `MoveOut`/`CopyValue` shape. The ledger reads what's emitted.
3. Closures and captured locals? The survey found `_active_captured_locals` in hir_to_mir. Proposed: treat captured locals as a separate ledger scope whose lifetime is the closure's; defer the full story to a 3B add-on.

## Deliverables for 3A (in order)

1. **This design note** — revised per K's feedback, awaiting second-pass sign-off.
2. `lang/driftc/stage2/ownership_ledger.py` — module with `LiveState`, `DropVerdict`, `LiveStateMap`, `build_ledger`, and `ledger.verdict_at(point, local) → DropVerdict`. Pure, no side effects, no env reads. Uses `DropPolicy` via injected callable.
3. `lang/driftc/stage2/ownership_ledger_events.py` — `DropDecisionEvent` dataclass + an append-only event log attached to `HIRToMIR` for sites 1/2's in-flight verdicts.
4. `lang/driftc/stage2/ownership_ledger_reporter.py` — compares decision events vs ledger verdicts (sites 1/2), exposes `check()` prospective API for sites 3/4, emits structured JSON records to stderr. Gated by `debug.enabled("ownership_ledger")`.
5. Decision-event recording at sites 1 and 2 (hir_to_mir.py — `_emit_scope_drops` and the match-arm cleanup loop). Each recording call short-circuits when the flag is off.
6. Prospective `ledger.verdict_at(...)` call-sites at sites 3 and 4 (string_arc.py — `_drop_all_destructibles` iteration + StoreLocal rewrite loop), each behind the debug-flag guard.
7. `lang/tests/stage2/test_ownership_ledger.py` — unit tests covering: straight-line flow, simple branch+join, `MoveOut` before scope exit, `StoreLocal` refines `MaybeUninit` back to `Live` (the K-refinement case), `MovedOut ∪ Tombstoned → MovedOut` (the K-classifier case), three-quadrant pin reproduction (dual-owner, POD variant, transient rvalue) — ledger must agree with current MIR for all three.
8. `work/ownership-ledger/triage.md` — populated after the `-n16` e2e run with `DRIFT_COMPILER_DEBUG='{"ownership_ledger":true}'` set; the input to the 3A→3B gate decision.

## Timeline reset (calendar, not effort)

- 3A: ~1 week (design note done, builder + reporter + sites + e2e observe + triage)
- 3C design note: ~2–3 days, in parallel with 3A's tail
- 3B: ~2 weeks (four consumer swaps, each with a focused regression + full e2e)
- Phase 4: ~1 week after 3B settles

Total: ~4–6 weeks calendar, confirming the earlier estimate.
