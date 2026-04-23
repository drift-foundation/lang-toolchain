# Phase 3C — Conditional-Move Lowering Design Note

Branch: `feature/ownership-ledger-rollout`
Status: **design CLOSED 2026-04-22; implementation strategy locked: runtime drop flags (RAII-preserving).**
Predecessor: 3A observational ledger (`design.md`, `4b-smoke-observations.md`); 3A→3B gate criteria require this note.

## Three-line summary

1. **RAII / destructor timing is source-observable.**  Implicit destruction is an ordered source semantic; cleanup runs at the local's scope-exit / unwind point.
2. **Runtime drop flags are the correctness baseline.**  For path-dependent destructible locals, the compiler emits a per-local Bool flag, sets it on assignment, clears it on move, and emits a flag-guarded drop at the original source scope-exit point.
3. **CFG-split is future optimization only**, gated on a proven reorder-safe `DropPolicy` axis.  NOT part of the minimum-viable 3C.

---

## Language contract on destructor timing

Implicit destruction is an **ordered source semantic**.  A destructor for a local runs at the local's scope-exit / unwind cleanup point if the local is initialized and not moved.  The compiler may omit or simplify cleanup only when it proves the destructor has no observable ordering effect, but it may not generally move cleanup earlier.

**Why this contract.**  Destructors in Drift are commonly used for RAII: mutex unlock, transaction rollback, span end, temp-file cleanup, refcount release.  These are observable, and users will write code that depends on cleanup happening at the source-level scope boundary (not at "last use," which the user does not see).  A "non-observable after last use" rule would let the compiler unlock a mutex or rollback a transaction earlier than the source scope exits — silently breaking RAII.  Drift supports RAII; therefore implicit destructor timing is source-visible at scope exit.

What the compiler **may** do:

1. **Omit** cleanup when it proves the local was definitely moved on every reaching path.
2. **Inline** cleanup as a no-op when the type's destructor is provably side-effect-free (POD types, refcounted scalars where the refcount itself is the only observable, etc.).
3. **Optimize** cleanup placement when source semantics are demonstrably preserved.

What the compiler **may NOT** do:

- Move a destructor earlier than the source-level scope-exit point of the owning local in a way that changes observable ordering.  This rules out the naive "push drop into predecessor edges" CFG-split for locals whose destructors have observable side effects.

## Implementation strategy: runtime drop flags

### What "path-dependent" means in current Drift

A local is path-dependent (the 3A ledger marks it `MaybeUninit` at some program point) iff its ownership state diverges across CFG arms reaching a join.  In current Drift, the **only** way this can arise for a destructible local is through a user `move L` expression on some arms but not others:

- Drift's grammar requires every `let`/`var` declaration to carry an initializer (`let_stmt: binder binding_name type_spec? alias_clause? EQUAL expr` in `lang/driftc/parser/grammar.lark`).  There is no syntax for uninitialized declarations, so a destructible local cannot be `Uninit` at any point past its declaration.
- Without uninitialized declarations and without user moves, every destructible local is `Live` from declaration to scope-exit.  Not path-dependent.
- A user `move L` is the only construct that can transition a Live local to a non-Live state mid-function on some arms but not others.

This means **two criteria are equivalent in current Drift**:

1. (design-level) The local has `MaybeUninit` raw state at some program point per the 3A ledger AND `DropPolicy.needs_drop = True`.
2. (implementation-level) The function contains at least one `MoveOut(_, L, _)` whose dest is consumed by something OTHER than an immediately-following `DropValue` (a "user moveout") AND the ledger reports `Live` or `MaybeUninit` at the pre-terminator point of at least one `Return` block.

The implementation adopts criterion (2) because it is mechanically simpler to test and avoids the false-positive trap from compiler-internal `MaybeUninit` artifacts at `Unreachable` blocks (e.g. match-dispatch chain dead-ends, see `lang/driftc/stage2/drop_flags.py::_is_potentially_live_at_some_exit` rationale).  Both criteria pick out the same set of locals on the current language.

If Drift later gains uninitialized declarations, the equivalence breaks: a `var x; if cond { x = make() }; ... use(x);` shape would have a path-dependent destructible local with NO user move, and criterion (2) would miss it while criterion (1) would catch it.  At that point the pass should switch to criterion (1), and the test in `lang/tests/stage2/test_drop_flags.py::test_path_dependent_no_user_move_currently_unrepresentable` (a regression that currently asserts the shape is not constructible in Drift) would flip to a positive flag-needed assertion.

### The pass

For each local where the language-equivalent path-dependence criteria above hold:

1. Allocate a Bool flag local at function entry.  Init to `true` if the local is a function parameter (params are initialized at entry); init to `false` otherwise.
2. After every `StoreLocal(local, _)`: insert `StoreLocal(flag, true)`.
3. After every `MoveOut(_, local, _)`: insert `StoreLocal(flag, false)`.
4. At every `Return` terminator (Unreachable terminators are statically dead — see filter rationale): emit a flag-guarded drop sequence:

       if flag {
           MoveOut(tmp, local)
           DropValue(tmp)
       }

   structurally an `IfTerminator` on the flag plus a drop-block plus a join-block.

For locals that do not satisfy the path-dependence criteria, the existing function-wide `_moved_locals` model is correct.  No flag needed; current emission stays.

**Cleanup runs at the original source scope-exit point.**  This preserves RAII: a Mutex unlock fires at the same source-syntactic location whether the local was initialized via the if-then arm or the if-else arm; the flag only governs *whether* cleanup runs, not *when*.

### Interaction with legacy unconditional scope-drop emission

When HIR→MIR's existing `_emit_scope_drops` already emitted a `MoveOut(t, L) + DropValue(t)` pair in a Return block for a flagged local, the pass detects this via `_block_already_drops` and **skips inserting a new flag-guarded drop in that block** — adding one would double-drop on every path.

Under the language constraint (Drift requires initializers), this skip is sound: legacy emits an unconditional scope-drop iff the local was never moved (function-wide `_moved_locals` is empty for L).  But criterion (2) requires the local to have a user moveout to qualify for flagging in the first place.  These two conditions cannot both hold for the same local: a local with a user moveout has `_moved_locals[L]` set, so legacy will skip the scope-drop, so `_block_already_drops` will be False, so the pass will insert the flag-guarded drop.  No double-drop is reachable.

**If Drift gains uninitialized declarations** and the equivalence above breaks, the skip-on-existing-drop heuristic also breaks: legacy would emit an unconditional drop for a path-dependent uninit-on-some-arms local, and the pass would skip flagging it, leaving the unsafe unconditional drop in place.  A future PR moving to criterion (1) MUST also replace `_block_already_drops`-skip with a `_block_already_drops`-then-strip-and-replace pattern, AND add a regression for the uninit-on-some-arms shape.  Pinned by `test_drop_flags.py::test_skip_on_existing_drop_under_user_move_invariant`.

## Where the work goes

New module: `lang/driftc/stage2/drop_flags.py`.

Pipeline placement: between HIR→MIR and `string_arc`, gated on whether the function has any path-dependent destructible local.  When none (the common case per Task #5 triage — 29 path-dependent records out of 9757 unique decisions, all in `main` functions on user locals), the pass is a no-op and skipped after the cheap initial scan.

Contract:

```
def insert_drop_flags(
    func: M.MirFunc,
    *,
    type_table: TypeTable,
    drop_policy: Callable[[TypeId], DropPolicy],
) -> M.MirFunc:
    """Insert per-local runtime drop flags for path-dependent
    destructible locals, so cleanup at scope-exit runs only on paths
    where the local is initialized and not moved.

    Algorithm (matches the equivalence stated in
    "What 'path-dependent' means in current Drift" above):
      1. Build a fresh LiveStateMap on the input MIR.
      2. For each named local L declared in the function (skipping
         compiler-internal `__`-prefixed locals — they have specialised
         handling in `string_arc`), include L iff BOTH:
           (i)  `DropPolicy(local_type(L)).needs_drop == True`, AND
           (ii) `L` has at least one **user moveout** — a
                `MoveOut(_, L, _)` whose dest is consumed by something
                OTHER than an immediately-following `DropValue`
                (excludes the scope-drop `MoveOut(t, L) + DropValue(t)`
                pattern), AND
           (iii) the ledger reports `Live` or `MaybeUninit` at the
                pre-terminator point of at least one **Return** block
                (Unreachable terminators are statically dead and
                excluded — see `_is_potentially_live_at_some_exit`
                rationale in the code).
      3. For each included local L:
         a. Allocate a Bool flag local.
         b. Insert flag-init at function entry: `true` if L is a param
            (params are initialized at entry); `false` if L is a
            declared local.
         c. After every `StoreLocal(L, _)`: insert flag-set-true.
         d. After every `MoveOut(_, L, _)`: insert flag-set-false.
         e. At every **Return** terminator block whose original
            instructions do NOT already drop L (see soundness
            invariant below): insert a flag-guarded drop ahead of the
            terminator, structurally an `IfTerminator` on the flag
            plus a drop-block plus a join-block.
      4. Return the rewritten function.

    The pass DOES NOT move drops earlier than their source-syntactic
    scope-exit point.  It does NOT touch locals that fail criteria
    (i)–(iii).  Cleanup runs at the original source scope-exit point;
    the flag governs *whether*, not *when*.

    Soundness invariant (skip-on-existing-drop): for any local L that
    qualifies under (ii), legacy `_emit_scope_drops` has already
    skipped emitting a drop for L at every Return block (because the
    user moveout sets `_moved_locals[L]` function-wide).  Therefore
    `_block_already_drops(blk, L)` is always False, and the pass's
    skip-vs-insert decision always inserts.  Pinned by
    `test_skip_on_existing_drop_under_user_move_invariant`.
    """
```

## Acceptance criteria

This pass MUST handle the two carrier shapes documented in `bucket6-known-bug.md`.  Both are checked in as `@pytest.mark.ledger_3c_acceptance` regressions in `lang/tests/stage2/test_hir_to_mir_path_insensitive_moved_locals.py`; landing 3C turned them passing **without rewriting stdlib**.  As of 0.31.8, the marker no longer deselects from default — these tests run in the standard suite.  The marker is preserved as a tag for targeted invocation: `pytest -m ledger_3c_acceptance lang/tests/stage2/` runs only these to confirm the bucket-6 acceptance contract.

1. **Terminating-arm leak** — `if b { return move s; } return "fresh";`.  The no-move return path must drop `s`.  Today's `_moved_locals` poisons function-wide and skips the drop → leak.
2. **Non-terminating conditional move** — `if b { val t = move s; } return "fresh";`.  The trailing scope-exit must drop `s` only when `b` was false.  Today's `_moved_locals` cannot represent path-dependent state → wrong on at least one runtime path.
3. **No stdlib rewrites required.**  Patterns like `std.cli::ArgParser.parse` (`inline_value`) and `std.containers/array.drift` (`k`, `v`) rely on user-level invariants — `has_inline ⇔ inline_value-is-dynamic`, `slot-is-occupied ⇔ k-and-v-are-live` — that the compiler cannot statically verify.  These are valid Drift; the pass must compile them.  An attempted strict fail-stop on disagreeing-arm move state was prototyped and rejected because it broke 8 stage2 tests on these patterns.

## Pinning tests

`lang/tests/stage2/test_drop_flags.py`:

- **Positive — terminating-arm leak shape**: `var s; if b { return move s; } return "fresh";` — the no-move return path emits a flag-guarded drop.  After the pass, the b=false runtime path drops `s`; the b=true runtime path does not (returned before scope-exit).  Flips `test_path_insensitive_moved_locals_omits_drop_on_no_move_path` to passing.
- **Positive — non-terminating conditional move shape**: `var s; if b { val t = move s; } return "fresh";` — the trailing scope-exit emits a flag-guarded drop.  On b=true: flag is false (cleared by move) → drop skipped → no double-drop.  On b=false: flag is true → drop runs → no leak.  Flips `test_non_terminating_conditional_move_no_silent_wrong_mir` to passing.
- **RAII invariant**: a function with a destructible local conditionally moved emits exactly one cleanup at the source scope-exit point on the no-move path, zero on the move path; cleanup fires at the same source-syntactic location regardless of which path was taken (no early cleanup).
- **No-op invariant**: a function with no path-dependent destructible locals is byte-identical post-pass.
- **Param init invariant**: a destructible param that is conditionally moved gets a flag initialized to `true` (params are live at entry).

## Acceptance gate (3C → 3B)

3B may begin only after **all** of:

1. `insert_drop_flags` lands and unit tests pass.
2. Default `pytest lang/tests/stage2/` is green (no regressions from the new pass).
3. `pytest -m ledger_3c_acceptance lang/tests/stage2/` is green (both carrier regressions flip).
4. Full e2e observe re-run shows bucket 6 (`real_disagreement`) at 0.
5. No new bucket-5 / bucket-6 class introduced (the runtime-flag plumbing must not surface as a fresh ledger disagreement category).
6. No stdlib rewrites required to satisfy any of the above.

## What 3C does NOT touch

- The 5-state `LiveState` lattice.  `MaybeUninit` is still a valid raw state for observation and for diagnostics; the pass *consumes* it (uses MaybeUninit as the path-dependent signal) but does not change the lattice.
- `DropPolicy`.  Same five axes; same funnel.
- 3A's recording / comparison machinery.  The reporter still classifies as today.  Bucket 3 (`path_dependent`) records will continue to surface for non-destructible path-dependent locals; that's diagnostic, not a pass failure.
- HIR→MIR sites 1/2 and `string_arc` sites 3/4.  3B owns the future ledger-driven swap of those emission sites; 3C is a separate, earlier pass that fixes the bucket-6 bug class so 3B's swap is uniform.
- Mainline `_moved_locals`.  Off-limits outside this track; the bug class is fixed by the new pass, not by patching the legacy set.

## Open questions (not blocking implementation)

1. **Loop-spanning conditional moves.**  Task #5 confirmed: zero such cases in current e2e (all 29 path-dependent records are if/match-join shapes in `main` functions, no loop bodies).  The pass should handle the common case correctly today; if a loop-spanning case ever surfaces, the flag mechanism naturally extends (the flag is per-local-per-function, not per-iteration).
2. **Diagnostic surface.**  When the pass emits flags+guards the user did not write, do we surface a debug-only annotation? Suggested: yes, behind `DRIFT_COMPILER_DEBUG='{"drop_flags":true}'`, mirroring the ledger gate.
3. **Phase 4 alignment.**  Tombstone fusion will need to interact with flag-guarded drops; the current design keeps `MoveOut` / `DropValue` shapes unchanged inside the flag-guarded block, so Phase 4's fusion analysis can recognize them.

## Why not start with the terminating-arm optimization

Tempting alternative: special-case the terminating-arm shape (one arm diverges) by using the surviving arm's `_moved_locals` state as the post-join state, avoiding flag overhead for that case.  Rejected for the first 3C landing:

- The terminating-arm intersection optimization is exactly how we got tempted into partial `_moved_locals` fixes.  Even if locally valid, it creates a second semantic path to review.
- The first 3C patch should be boring and uniform: one rule, applied to every path-dependent destructible local, no exceptions.
- Optimization comes after the baseline is correct and observed.  When `DropPolicy.is_reorder_safe` lands, both the terminating-arm case and the broader CFG-split case become optimizations layered on top of the runtime-flag baseline.

---

## Historical context (superseded — preserved for the record)

The earlier draft of this doc picked CFG-split / drop-elaboration as the default and runtime flags as a fallback, justified by low `path_dependent` volume in Task #5 triage.  That justification holds for *performance* but not for *correctness* once destructor side effects are observable at scope-exit.

The original three-answer destructor-order analysis:

> **Answer A — destruction timing within a function is non-observable.**  CFG-split is sound for any local that satisfies the use-safety invariant.  Pass scope: full.  *(Rejected — would silently break RAII for Mutex unlock, transaction rollback, etc.)*
>
> **Answer B — destruction timing is observable in general; only side-effect-free destructors may be reordered.**  Pass restricts itself to types with a future `DropPolicy.is_reorder_safe = True` axis.  *(Reserved as a future optimization layered on top of the runtime-flag baseline.)*
>
> **Answer C — destruction timing is observable AND elaboration must preserve source-order destruction.**  CFG-split is unsound in general.  The pass falls back to per-local runtime drop flags for every `PathDependent` case.  *(This is the chosen contract — see "Language contract on destructor timing" above.)*

The original CFG-split implementation plan included a "use-safety invariant on predecessor edges" requirement: a drop may only be pushed into a predecessor when the local has no later uses on that edge.  This invariant remains correct **as a constraint on any future CFG-split optimization that may land**; a future PR adopting CFG-split MUST satisfy both the use-safety invariant AND the reorder-safe type policy axis before landing.  Without the latter, CFG-split silently breaks RAII even when use-safety holds.

The original "Two strategies" / "Decision" / "Where the work goes (CFG-split version)" sections are dropped in this rewrite.  See git history for the prior content if needed.
