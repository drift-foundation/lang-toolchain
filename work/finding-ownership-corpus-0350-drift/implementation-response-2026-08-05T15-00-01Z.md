# Baton message

Timestamp: 2026-08-05T15-00-01Z
From role: implementer
Actor: k
To role: reviewer
Kind: implementation
Thread: ownership_corpus_0350

Responds to the exact incoming claim:

`CLAIMED-FROM-reviewer-TO-implementer-2026-08-05T14-53-36Z-72d5cfd30bbf-BY-k-SEED-46dae92ab18cc9f2a91412ac92fee356-AT-2026-08-05T14-53-36Z`
Independent corpus analysis complete — full record in this finding's
PROGRESS.md.  Headline: the one-function delta is fully explained, tied to
a reviewed intended change, and I recommend PROMOTE UNCHANGED — with one
correction to the provisional interpretation.

The missing function, BY NAME (not inferred from counts): the audit's
per-fn records (DRIFT_STRING_ARC_AUDIT verbose mode) give a name-exact
inventory diff for closures_share_capture_eval_order between a read-only
archive tree at baseline commit 2b81980d and the current tree — exactly
one line:

  std.core::std.core.arc.Arc<T>::Borrow<T>::borrow__inst__703503d992a3c867

1286 → 1285, no other name differs.  So it is a dead Arc Borrow-impl
MONOMORPH, not hidden-lambda output — that part of the provisional
interpretation was wrong, though its "phantom compiler-generated function"
instinct was right.

Phantomness proven two ways: the baseline .ll DEFINES the symbol but
contains ZERO calls to it, and its baseline audit record carries
events: 0 (exactly the counters-unchanged hazard your review flagged —
resolved by the named inventory, not the counters).

Causing change, by archive-tree bisection across the 0.35.0 train: the
instantiation is still present at f150cbce (1286) and gone at 0c9413dd
(1285) — "callsite side tables partitioned by finalized-body ownership".
The partition stops non-owned callsite-indexed instantiation records from
leaking into the parent TypedFn's emission set; this phantom was exactly
such a leak.  Intended, reviewed, and pinned behavior
(test_nested_lambda_callinfo_ownership.py).

No loss, no instability: the fixture compiles and RUNS exit 0 (its
expected result) on both the baseline archive and the current tree; two
independent current-tree compiles reproduce 1285 with identical name
lists, and my recomputation matches the retained actual's projection
exactly — which also corroborates (in lieu of the terminal text I don't
have) that nothing exists beyond your comparison's list.

Recommendation (your item 6): promote unchanged.  Semantic delta =
intended dead-instantiation removal by 0c9413dd; the rest is
identity/metadata (0.34.2→0.35.0 digests, timestamps) plus the db8886ab
comment-hash change you already identified.  Baseline untouched;
promotion not run — awaiting your concurrence and Slawomir's go.
