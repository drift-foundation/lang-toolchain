# Site 3 strings/arrays return-source migration — kickoff

Carryover from `feature/ownership-authority-finale` (closed at 0.31.13,
ABI 10).  This is the remaining piece of the ownership-ledger
authority unification: site 3's `string_arc_return` branch still uses
a legacy alias-walk for `String` / `Array<…>` return-source cleanup
suppression.  Destructibles (sub-step 1), destructor-method `self`
(sub-step 2), and variant zero-tag widening (sub-step 3) all migrated
to the ledger in 0.31.9 and remain in place.

## What's already done (in tree, do NOT redo)

| Sub-step | Status | Mechanism |
|---|---|---|
| Returned-value source — destructibles | LANDED 0.31.9 | `verdict_at` consultation at the Return cursor; composite constructors (`ConstructStruct` / `ConstructVariant` / `ConstructResultOk` / `ConstructIfaceValue`) feeding `Return` are lattice-modelled as Return-as-move (args' source locals → MOVED_OUT at LoadLocal index). |
| Destructor-method `self` skip | LANDED 0.31.9 | Lattice transitions `self` to MOVED_OUT at every Return-terminator block in destructor methods (predicate on `fn_id.name`). |
| Variant zero-tag widening | LANDED 0.31.9 | `verdict_at == PathDependent` + `variant_zero_tag_drop_safe(ty, type_table)` policy axis — one helper, no inline conditionals. |

## What remains

Site 3's Return-terminator branch in `lang/driftc/stage2/string_arc.py`
still consults a **site-local alias walk** for `string_locals` and
`array_locals` to decide whether the returned value's source local
should be skipped from cleanup.  The relevant code paths:

- `_collect_return_source_locals(val)` — recurses through `AssignSSA`
  and `LoadLocal` to find the named local feeding a return value.
- The Return branch reads `string_locals` / `array_locals` and uses
  the alias walk to add to `skip_cleanup_locals`.
- `can_move_from_skipped_local` — downstream string-ownership transfer
  at the actual return value.  Separate concern from cleanup
  decisions; SHOULD STAY when the cleanup path moves to the ledger.

## The blocker — why this didn't land in the finale

Sub-step 1 was scoped narrowly to **destructibles** because broader
consultation (folding strings/arrays into the same `verdict_at` loop)
broke two memcheck carriers:

- `lang/tests/memcheck/test_scope_drop_conditional_move.py`
- `lang/tests/memcheck/test_pkg_map_literal_string_leak.py`

The standard stage2 / acceptance / driver matrix did **not** catch
those — only memcheck did.  The fix at the time was to narrow the
ledger consultation to `destructible_locals` and restore the
alias-walk skip for strings/arrays.  See
`work/ownership-ledger/site3-authority-completion.md` (in the
finale branch's pre-cleanup state) for the post-landing honesty
correction.

## Hard constraint for resuming this work

**Memcheck must be in the standard verification gate from the FIRST
patch.** Per the standing project memory
(`feedback_memcheck_in_gate.md`):

> Site-3 / `skip_cleanup_locals` patches MUST run
> `lang/tests/memcheck/` in the verification matrix from the start;
> stage2 / acceptance / driver miss string/array ownership-tracking
> interactions.

The minimum verification matrix per patch attempt:

```
PYTHONPATH=. .venv/bin/python -m pytest lang/tests/stage2/   -q -n 16
DRIFT_MEMCHECK=1 PYTHONPATH=. .venv/bin/python -m pytest lang/tests/memcheck/ -q -n 16
PYTHONPATH=. .venv/bin/python -m pytest lang/tests/driver/   -q -n 16
PYTHONPATH=. .venv/bin/python -m tools.ownership_observe.run_observe
PYTHONPATH=. .venv/bin/python -m tools.ownership_observe.aggregate_triage
```

Observe-sweep tooling lives in `tools/ownership_observe/` (proper
package, importable; carryover from the deleted scratch dir
`work/ownership-ledger/` after the finale branch closed).  Sweep
output and triage report at `build/ownership-ledger/triage/`.

Bucket 6 must remain 0; bucket 2 must remain 0; bucket 3 path-dependent
records (currently 2) should not grow without explanation.

## Touch points (file paths to start from)

- `lang/driftc/stage2/string_arc.py` — Return-terminator branch +
  `_collect_return_source_locals` + `can_move_from_skipped_local` +
  `string_locals` / `array_locals` set construction.
- `lang/driftc/stage2/ownership_ledger.py` — already has
  `_identify_return_consumed_loads` for the destructibles case;
  composite Return-as-move recognises `ConstructStruct` /
  `ConstructVariant` / `ConstructResultOk` / `ConstructIfaceValue`.
  May need lattice-side support for tracking strings/arrays
  return-source.
- `lang/tests/memcheck/test_scope_drop_conditional_move.py` and
  `lang/tests/memcheck/test_pkg_map_literal_string_leak.py` — the
  carriers that broke last attempt.  Treat as the regression gate.

## Suggested first-patch shape

1. **Pin the carriers as the gate** — confirm both memcheck tests
   pass on baseline before any changes.
2. **Add a regression carrier for the strings/arrays return-source
   pattern** the alias-walk currently handles correctly — the carrier
   should leak / UAF if the alias-walk is dropped without ledger
   replacement.  Run before code change; expect it to fail when the
   alias-walk is removed.
3. **Decide the model**: extend `_apply` (whole-local) to mark
   strings/arrays return-source locals MOVED_OUT at the LoadLocal
   index for return-feeding chains?  Or add a dedicated lattice axis
   for refcounted-scalar / array return-source tracking?  The
   destructibles case already handled composite constructors; the
   strings/arrays case may be implementable as a uniform extension
   (drop the type-class filter from the Return-as-move walker).
4. **Swap site 3's strings/arrays alias walk** to ledger consultation;
   keep `can_move_from_skipped_local` for the actual string-ownership
   transfer at the return value.
5. **Verification gate** — stage2 + memcheck + driver + observe.

## After this lands

Site 3 becomes Tier 1 (ledger-authoritative).  No site-local
authority surfaces remain except `flag_managed_locals` (deferred to
3C by design — not a split).  `_collect_return_source_locals` likely
deletable (only `can_move_from_skipped_local` keeps it alive today).

The Drift ownership-authority track is then structurally complete.
The only residuals from there are the three tombstone hardening pin
gaps surfaced in the finale's tombstone audit (codegen variant-drop
tag-routing pin; MIR boundary assertion pin; `ArrayElemTake` audit
pin) — non-blocking, can be backfilled at any time.

## Branch suggestion

`feature/site3-strings-arrays-tier1`

ABI bump: no (internal authority change, no runtime ABI shape
change).  Compiler minor bump: yes, when it lands.

## Carryover artifacts (if you want to re-import any)

The closed `feature/ownership-authority-finale` branch's working
notes are wiped per project convention (`work/*` is gitignored
scratch).  If any of these are worth re-importing, they were:

- `branch-closure-memo.md` — closure assessment for the prior branch
  (`feature/ownership-ledger-rollout`).
- `finale-closure.md` — final authority table for
  `feature/ownership-authority-finale`.
- `site3-authority-completion.md` — design note for sub-steps 1/2/3
  (already landed in 0.31.9; this kickoff is the **strings/arrays
  remainder**, NOT a redo).
- `3b-status.md` — honest tier table at Phase 3B.
- `design.md` — original ledger design.

The information in those notes that's load-bearing for THIS work is
captured above (the Sub-step 1 honesty correction; the memcheck-in-
gate constraint; the touch points).  The rest is historical context
for the previous branches and not needed to resume.
