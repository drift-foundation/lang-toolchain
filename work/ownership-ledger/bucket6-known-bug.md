# Bucket 6 — RESOLVED (Phase 3C runtime drop-flags, 0.31.8)

**Status:** **RESOLVED 2026-04-22 by Phase 3C runtime drop-flag insertion** (`lang/driftc/stage2/drop_flags.py`, compiler 0.31.8).  E2E observe re-run shows bucket 6 (`real_disagreement`) cleared 5 → 0.  Both `ledger_3c_acceptance`-marked carrier regressions in `lang/tests/stage2/test_hir_to_mir_path_insensitive_moved_locals.py` flipped red → green; the marker is no longer deselect-by-default and they run in the standard suite.

This document is preserved as the historical record of the bug class and the two failed-fix attempts that motivated the runtime-flag baseline.  See `work/ownership-ledger/3c-design.md` for the current design and `docs/history.md` 0.31.8 entry for the landing summary.

This document supersedes the earlier `bucket6-fix.md` (kept in git history for the failed-fix attempt record).

## The defect

`HIRToMIR._moved_locals` is a function-wide `set[str]` populated at every `HMove` lowering site and consulted by `_emit_scope_drops` to decide whether to skip a local's drop.  The set is **not** path-scoped.  Source `move s` semantics are path-local; the set's function-wide scope is unsound for any conditional move.

## Two carrier shapes

### 1. Terminating-arm leak (the bucket-6 trigger)

```drift
fn f(b: Bool) -> String {
    var s = "owned";
    if b { return move s; }
    return "fresh";              // ← b=false: s never moved; should be dropped
}
```

The `move s` in the terminating then-arm adds `s` to `_moved_locals`.  When the b=false return path's `_emit_scope_drops` runs, the poisoned set causes the drop to be skipped.  **One String allocation leaks per b=false call.**

At scale: `std.json::_parse_object_throwing.fields` (a `containers.HashMap`) leaks one HashMap per malformed-JSON parse failure.  Five distinct error-return blocks inside the parser's `while true` body all share the leak (Phase 3A Task #5 triage, bucket 6).

### 2. Non-terminating-arm conditional move (K-found 2026-04-22)

```drift
fn f(b: Bool) -> String {
    var s = "owned";
    if b { val t = move s; }   // moves s; arm reaches the join (no return)
    return "fresh";              // post-join: s is moved on b=true,
                                 // live on b=false
}
```

After the join, `s`'s state is genuinely path-dependent.  The function-wide `_moved_locals` cannot represent it.  Today the compiler silently emits MIR that either leaks (skip drop on every path) or double-drops (drop on every path) — both are wrong on at least one runtime path.

At scale: `std.cli::ArgParser.parse` (`inline_value`) and `std.containers.array` (`k`, `v`) rely on user-level invariants — `has_inline ⇔ inline_value-is-dynamic`, `slot-is-occupied ⇔ k-and-v-are-live` — that the compiler cannot statically verify.  Mainline "works" because the unmoved-arm runtime values happen to be static-pool literals (zero-cost no-op drop) on the paths actually reached.

## Why the bug is not patched on mainline

Two attempted fixes, both rejected:

- **Set intersection at HIf joins** (snapshot before each arm; intersect at the join with implicit-else = pre-state).  Cleared the bucket-6 leak shape.  Soundness gap: for the non-terminating-arm sibling shape, the post-join state collapses to "not moved" (intersection drops the moved fact) → emits unconditional drop → DOUBLE-DROP / UAF on the runtime path that actually executed the move.
- **Strict fail-stop on disagreeing reaching arms.**  Sound — refuses to compile any program where post-join move state is path-dependent.  Blocks 8 stage2 tests because legitimate stdlib code in `std.cli` and `std.containers` relies on the runtime invariants the compiler cannot prove.  Per the no-stdlib-rewrite policy, this is non-landable.

The bug class requires **per-program-point ownership state**, which only Phase 3C drop-elaboration provides.  No half-fix exists in the function-wide-set model.

## Containment plan (current)

1. **Mainline `_visit_stmt_HIf`** is unchanged from pre-3A behaviour.  Comment at the lowering site names the bug, points at this doc, and forbids future patch attempts that don't go through 3C.
2. **Two acceptance-criterion regressions** in `lang/tests/stage2/test_hir_to_mir_path_insensitive_moved_locals.py`, both tagged `@pytest.mark.ledger_3c_acceptance`.  This marker is **excluded from default `pytest`** by `pytest.ini`'s `addopts = -m "not ledger_3c_acceptance"` — keeping mainline CI green — and runnable explicitly with `pytest -m ledger_3c_acceptance lang/tests/stage2/`.  The marker is NOT `xfail`: an `xfail` says "failure is acceptable here," and that is the wrong framing.  These failures are **acceptance-blocking**; the feature branch is not done until both pass under the 3C ownership model.  An accidental mainline pass surfaces inside the test as a normal pass — drop the marker at that same commit.
3. **Phase 3A observational ledger continues to surface the bug at scale.**  Bucket 6 in the e2e triage is documented as expected-non-zero on mainline; the gate to 3B is "bucket 6 = 0 *after 3C lands*", not "bucket 6 = 0 today on mainline".
4. **No stdlib changes.**  The std.cli / std.containers patterns that depend on user-level invariants are not rewritten — 3C drop-elaboration must accommodate them.
5. **Phase 3B consumer swaps do NOT begin** until 3C has an implementation path that handles both carrier shapes.

## Direct input to 3C

This bug class is now a **functional requirement** of the 3C design:

- The drop-elaboration pass MUST handle the **terminating-arm leak** shape: when one arm of an HIf returns/throws and the other reaches the join, the join's drop authority must reflect the no-move arm's state (today: leak).
- The drop-elaboration pass MUST handle the **non-terminating conditional move** shape: when both arms reach the join with disagreeing move state, either insert an explicit drop on the live-side arm before the join (CFG-split with use-safety invariant — see `3c-design.md`) OR fall back to a per-local runtime drop flag (the targeted (1) fallback — not function-wide regression to runtime tracking).
- The pass MUST NOT require stdlib code rewrites to compile.  Patterns like `std.cli::inline_value` are valid Drift; the compiler must support them.

These three requirements are now first-class acceptance criteria for 3C, not nice-to-haves.

## Related artefacts

- `work/ownership-ledger/3c-design.md` — the design note (must absorb the carrier shapes as concrete acceptance criteria).
- `work/ownership-ledger/triage-findings.md` — the discovery context.
- `build/ownership-ledger/triage/triage.md` — auto-generated bucket counts (bucket 6 = 5 on mainline; will go to 0 once 3C lands and the ledger replaces `_emit_scope_drops`).
- `lang/tests/stage2/test_hir_to_mir_path_insensitive_moved_locals.py` — the carrier xfails.
