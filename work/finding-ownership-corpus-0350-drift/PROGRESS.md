# PROGRESS: ownership-corpus 0.35.0 drift — independent implementer analysis

Actor: K (implementer), 2026-08-05.  Method: named function-inventory
diffs via the ownership audit's per-fn records
(`DRIFT_STRING_ARC_AUDIT=1 DRIFT_STRING_ARC_AUDIT_VERBOSE=1
DRIFT_STRING_ARC_AUDIT_FILE=...`), compiling the single fixture
`lang/tests/codegen/e2e/closures_share_capture_eval_order/main.drift`
on the current tree and on read-only `git archive` trees (no checkouts,
no baseline edits, no promotion).

## Findings, keyed to the reviewer's six questions

1. Verifier terminal text: not available to me.  Corroboration instead:
   my independent current-tree recomputation reproduces the retained
   actual's projection EXACTLY (fns=1285, all other counters as
   retained), so no differences exist beyond the review's list for this
   fixture.
2. NAMED inventory diff (baseline commit 2b81980d archive vs current
   acb8c4ab tree): exactly ONE function —
   `std.core::std.core.arc.Arc<T>::Borrow<T>::borrow__inst__703503d992a3c867`
   — present at baseline (1286), absent now (1285).  No other name
   differs.
3. Causing change, by archive-tree bisection over the 0.35.0 train:
   present at f150cbce (1286), absent at 0c9413dd (1285) — i.e. the
   reviewed commit "inferred-lambda returns reconciled at the primary
   authority; callsite side tables partitioned by finalized-body
   ownership".  The partition stops non-owned callsite-indexed
   instantiation records from leaking into the parent TypedFn's
   emission set.  This is an intended removal of PHANTOM output — with
   one correction to the provisional interpretation: the removed
   function is a dead Arc Borrow-impl monomorph, NOT hidden-lambda
   output.
4. Phantomness and no-loss proof: the baseline .ll DEFINES the symbol
   but contains ZERO calls to it (`grep -c "call.*703503d992a3c867"` =
   0); its baseline audit record has `events: 0`.  The fixture compiles
   and RUNS exit 0 (its expected result) on BOTH the baseline archive
   tree and the current tree — no real callable body was lost.
5. Determinism: two independent current-tree compiles both yield 1285
   with identical name lists; the baseline archive yields 1286 with the
   single extra name.  No counting instability observed; the audit's
   fns counter bumps once per audited function, and the inventory diff
   is name-exact.
6. RECOMMENDATION: **promote unchanged**.  The single semantic delta is
   the intended dead-instantiation removal by reviewed commit 0c9413dd;
   the remaining bundle diffs are identity/metadata (0.34.2→0.35.0
   version and digests, run timestamps) plus the already-reviewed
   db8886ab fixture-comment hash change the reviewer identified.

## Evidence files (scratchpad, session-local)

current.jsonl / current2.jsonl (1285 fns, twice), base.jsonl (1286),
f150cbce.jsonl (1286), 0c9413dd.jsonl (1285), current_fns.txt /
base_fns.txt (single-line diff), fixture binaries (both run exit 0).
Commands recorded in the baton reply.
