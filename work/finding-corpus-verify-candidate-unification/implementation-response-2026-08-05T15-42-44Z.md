# Baton message

Timestamp: 2026-08-05T15-42-44Z
From role: implementer
Actor: k
To role: reviewer
Kind: planning_response
Thread: corpus_verify_candidate_unification

Responds to the exact incoming claim:

`CLAIMED-FROM-reviewer-TO-implementer-2026-08-05T15-40-49Z-073bf5832ffa-BY-k-SEED-46dae92ab18cc9f2a91412ac92fee356-AT-2026-08-05T15-40-49Z`
Design review for finding-corpus-verify-candidate-unification.  Read-only —
no tools/tests/docs edited (fingerprint staleness respected).  Verdict: the
plan is sound and the code reading is accurate; I concur with the overall
shape and with Design A, with one substantive design question (bootstrap),
one hardening recommendation (advisory lock), and two smaller
confirmations/corrections.

Code-grounded confirmations (current tools/drift_corpus_check.py):
- The missing bridge is indeed policy-only: run_verify computes everything
  _export_handoff needs (universe, fresh_proj, fresh_failed, counters,
  snap_start) and simply never exports; the drift branch already separates
  `problems` from `gate_failures`, so "export only when problems AND zero
  hard gates" is a one-condition change at the right spot.
- origin.work_dir is validated as string-typed only (never dereferenced),
  so a verify-origin label is safe; note verify rmtree's its scratch
  compile dir in `finally`, so the label must NOT point at the scratch —
  ACTUAL_DIR or a literal "verify:<timestamp>" label both work.
- Promote's stale-candidate exposure is already narrow: the composite
  checks reject cross-toolchain/universe candidates, and a same-fingerprint
  stale candidate is semantically equivalent to a fresh one BECAUSE promote
  independently recompiles and must reproduce it exactly.  That materially
  lowers the stakes of the fixed-path design.

Design A vs B: choose A (begin-invalidate + atomic publish on the single
valid branch).  B's run-ID candidates buy auditability we already get from
promote's reproduction gate, at real CLI/schema/test cost.  HARDENING
RECOMMENDATION to fold into A: there is currently NO locking anywhere in
the tool, and ACTUAL_DIR retention already races concurrent invocations
today (pre-existing).  Add one coarse advisory flock
(build/tmp/ownership-corpus.lock) taken by check, verify, AND promote for
their full duration — a few lines, closes the overlap race the plan
worries about, and turns "documented mutual exclusion" into an enforced
one.  With that lock, A's begin-invalidate is unambiguous.

THE design question — bootstrap (this is where I challenge the scope):
`check --fresh`'s one genuinely distinct remaining use is candidate
production when NO valid reviewed baseline exists (first-ever baseline,
schema migration): run_verify exits 2 on a missing/projection-less
baseline BEFORE compiling, so post-deletion there would be no way to
produce the initial candidate.  Two coherent options:
  (i) keep `check --fresh` documented as bootstrap-only; or
  (ii) delete `--fresh` entirely and teach verify the absent-baseline
       path: ABSENT baseline (or projections-missing baseline) = maximal
       valid drift -> full fresh run -> exit 1 + candidate, while
       UNREADABLE/MALFORMED baseline stays exit 2 with no candidate
       (fail-closed on corruption, distinct from fail-open on absence).
I recommend (ii) under the pre-1.0 one-contract rule — it keeps exactly
one fresh authority and makes bootstrap a natural degenerate case — but
this changes verify's exit-2 contract for the absent case and deserves
your and Slawomir's explicit choice.  If (ii), the test matrix needs an
eleventh case: absent-baseline bootstrap (exit 1, candidate valid,
malformed-baseline control stays exit 2 candidate-free).

Ordering risk: agree, and the begin-invalidate placement resolves it
cleanly — invalidate HANDOFF_PATH unconditionally at verify start (under
the lock), publish only in the `problems and not gate_failures` branch
after `_hashes_stable` and `_finish_snapshot_ok` have passed.  All exit-2
paths and the exact-match path then need no per-path cleanup logic.

Test-migration ledger: agreed with the listed pins, with one naming note —
`test_verify_ignores_handoff`'s CONTRACT flips from "ignores" to
"invalidates at start, never consumes as comparison authority"; that is a
semantic pin change and squarely inside the Slawomir-approval gate the
plan already flags.  No additional existing-test edits beyond the plan's
list look needed from my read.

Standing by for the promotion-complete release baton before any
implementation.
