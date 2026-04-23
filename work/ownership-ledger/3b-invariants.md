# Phase 3B Consumer-Swap Invariants

This document captures the cross-cutting invariants every 3B step must respect, derived from K's review of step 1 (`drop_before_overwrite` swap, 0.31.8).

## Invariant 1 — Ledger build-timing

The driver builds the ownership ledger **before** the `drop_flags` pass runs:

```
HIR → MIR
  ↓
build_ledger(func)        ← attached as func._ownership_ledger
  ↓
drop_flags pass            ← may add blocks + inject flag instructions
  ↓
string_arc pass            ← consumes func._ownership_ledger
```

This means **every consumer-swap site receives the PRE-`drop_flags` ledger.**  That is correct iff the consumer's drop decision depends only on MIR state that `drop_flags` does not mutate.

`drop_flags` mutates MIR in three ways:

1. **Adds Bool flag locals** to `func.locals` (new `__drop_flag_<L>` entries).
2. **Inserts flag-set/clear instructions** immediately adjacent to existing `StoreLocal(L, _)` and `MoveOut(_, L, _)` operations for flagged locals.
3. **Splits Return-terminator blocks**: original block keeps its instructions, gains a `LoadLocal(flag) + IfTerminator` ending; new `<orig>_drop_<L>` blocks contain `MoveOut + DropValue + Goto`; new `<orig>_dropfinal` blocks carry the original Return.

A consumer is safe to consume the pre-`drop_flags` ledger iff its decisions reference program points that **survive unchanged** through the three mutations above:

- Original block names are preserved (the rename happens in suffix-derived new blocks).
- Original instruction indices within original blocks are preserved (flag-set/clear are appended AFTER the StoreLocal/MoveOut they shadow, so prior instruction positions are stable).
- The PRE-Return state of the original Return block becomes the PRE-`LoadLocal(flag)` state after `drop_flags` (the post-`drop_flags` block has the flag load appended at the tail).

### Per-consumer audit

| Consumer site | Pre-3B ledger correct? | Notes |
|---|---|---|
| `drop_before_overwrite` (site 4) | ✅ yes | Decision is at a `StoreLocal(L, _)` point in the original block; flag-set/clear are inserted AFTER that point, so the pre-state at the StoreLocal is unaffected by drop_flags.  Step 1 landed under this analysis (no observe-bucket regression). |
| `string_arc_return` (site 3) | ⚠️ **NO — needs verification before swap** | Site 3 emits drops at function-exit Return blocks for destructible locals.  After `drop_flags` runs, the original Return block's terminator is replaced with an `IfTerminator(flag)` and the actual Return moves to a `<orig>_dropfinal` block.  Consuming the pre-`drop_flags` ledger means site 3 sees the original Return's pre-state — but the actual scope-exit on the post-`drop_flags` MIR is at `<orig>_dropfinal`, not the original block.  Site 3 also risks DOUBLE-DROPPING flagged locals (3C's `_drop_<L>` block + site 3's drop on the same local at the post-pass Return).  **Step 2 must address this**: either rebuild the ledger after `drop_flags`, or have site 3 detect "this local has a 3C flag" and skip emission for it. |
| `scope_drop` (site 1) | n/a until step 3 | This site runs INSIDE HIR→MIR, before the ledger exists at all.  The 3B consumer-swap pattern (post-build ledger consultation) doesn't apply directly; this site needs a different approach (or HIR→MIR itself learns to consult an in-flight ledger). |
| `match_cleanup` (site 2) | n/a until step 4 | Same as site 1: runs inside HIR→MIR.  Plus the per-field gap (3A bucket 1) blocks ledger-driven decisions until per-field state lands. |

### Required action before step 2 (`string_arc_return` swap)

Pick one of:

1. **Rebuild the ledger after `drop_flags`** for site 3's consumption.  Cheap (worklist dataflow).  Pro: site 3 gets accurate per-program-point state on the final MIR shape.  Con: extra build per function.
2. **Have site 3 skip emission for flagged locals** by checking `func.locals` for the `__drop_flag_<L>` marker.  3C is then the sole authority on those locals' scope-exit drops.  Pro: no extra build, clean responsibility split.  Con: introduces a coupling between site 3 and `drop_flags`'s naming convention.
3. **Move `drop_flags` to run AFTER `string_arc`** so site 3 sees the pre-flag MIR.  Pro: site 3 unaffected by 3C.  Con: `drop_flags` would need to wrap site 3's emissions in flag-guards, not just emit its own — bigger pass change.

**Recommendation**: option 2 for step 2 (smallest patch, cleanest split), with a regression that builds a function with a flagged local AND a destructible local needing site-3 cleanup at the same Return — assert no double-drop on the flagged local AND drop emitted for the unflagged local.  Confirm via observe re-run that no new bucket-5/6 class arises from any site-3 / drop_flags interaction at e2e scale.

## Invariant 2 — Site authority status

Per K's review wording: each site's status is one of:

- **Pre-swap (legacy authority)** — site emits decisions from its own dataflow; the ledger is observed in parallel via the 3A reporter but does not influence emission.
- **Partial swap (ledger-authoritative for deterministic verdicts; legacy fallback retained)** — site reads the ledger for `MustDrop`/`MustNotDrop` and falls back to legacy for `PathDependent` or unavailable-ledger.  Site-local authority is NOT fully removed.
- **Full swap (ledger sole authority)** — site has no fallback; ledger handles all verdict cases.  Requires either drop-before-overwrite-style flag plumbing at the site OR a proven-zero `PathDependent` count at e2e scale.

| Site | Status |
|---|---|
| `drop_before_overwrite` | Partial swap (step 1, 0.31.8) |
| `string_arc_return` | Pre-swap |
| `scope_drop` | Pre-swap |
| `match_cleanup` | Pre-swap |

A "site swap step" lands one site from one status row to the next.  Full swap requires its own justification per site (see invariant 3).

## Invariant 3 — Removing legacy state

Legacy site-local state (`initialized_destructibles`, `_moved_locals`, `arm_scrut_payload_moved`, etc.) MUST NOT be deleted while ANY consumer with a partial-swap status still consults it.  Legacy state is retired only when:

- All consumers are in "full swap" status, AND
- Phase 4 cleanup begins (per K's directive: "Do not delete legacy state until all consumers are swapped and Phase 4 cleanup starts").

Until then, the legacy data structures are computed on every run, even when their values would be ignored by ledger-authoritative branches.  This costs a small amount of CPU per function but preserves the fallback path and the bisect surface for any swap-introduced regression.

## Invariant 4 — Observe mode preserved during and after swaps

The observe-mode telemetry path (`_ledger_reporter.check(...)` calls gated by `debug.enabled("ownership_ledger")`) must remain functional after every swap step.  Observe re-runs are how we confirm "no new bucket-5/6 class" gate criteria at each step.

A swap that silently drops the observe path (e.g. by removing the `check` call, or by emitting records that no longer match the bucket-5/6 detector rules) is a swap that hides regressions.  Pinned per swap by a `test_swap_emits_observe_records_when_flag_on`-style test in the swap's targeted regression file.
