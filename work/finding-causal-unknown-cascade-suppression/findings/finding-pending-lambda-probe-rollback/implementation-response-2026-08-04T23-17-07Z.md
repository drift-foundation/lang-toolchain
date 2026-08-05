# Implementation handoff

Timestamp: 2026-08-04T23-17-07Z
Role: implementer
Actor: k

Responds to the exact incoming claim:

`CLAIMED--REVIEW-PENDING-2026-08-04T22-51-46Z--BY-k--SEED-46dae92ab18cc9f2a91412ac92fee356--AT-2026-08-04T22-51-46Z`
# Implementation handoff: mutation-site pending-lambda barrier IN TREE (K)

Author: K (implementer), actor `k`, seed `46dae92ab18cc9f2a91412ac92fee356`

Responds to the claim recorded in the baton receipt for
`REVIEW-PENDING-2026-08-04T22-51-46Z` (target sha256
65be81803cc60dec91e32d4c51da497aefa647d2e77bdd34bd3ab7045566dcd8).

The mutation-site barrier is implemented per all six constraints; the
complete record (design→diff mapping, red-first proof against a
read-only pre-barrier scratch tree, gate results incl. one name-lint
iteration resolved by renaming the owner's consume→retire) is in the
child's PROGRESS.md:
`finding-causal-unknown-cascade-suppression/findings/finding-pending-lambda-probe-rollback/PROGRESS.md`

Diff surface (uncommitted, on the pending 0.35.0 train, ABI 22):
- lang/driftc/checker/call_resolver.py — PendingLambdaBarrier
  (BaseException) + probe handler (nested re-raise / outermost silent
  deferral) + documented counters deferrals_pending_barrier /
  pending_barrier_nested;
- lang/driftc/type_checker.py — PendingLambdaOwner (module-level;
  private map; register/peek/begin_resolution/retire/drain), the five
  former direct-map sites rewired, owner wired to the live
  FnCheckState._txn_depth;
- lang/tests/checker/test_pending_lambda_probe_barrier.py (NEW) — full
  state-identity audit (red-first proven), B5 COMPLETE control
  (compile+run), owner unit contract, exact-id/shadowing pins,
  nested-gating pin over real FnCheckState/CheckerStateTxn;
- doc/history.md — paragraph folded into the pending 0.35.0 entry.

Gates: focused battery 91 passed; compiler suites 1120 passed;
barrier module 5/5; git diff --check clean.  Ready for static review.
