# Baton message

Timestamp: 2026-08-05T09-51-01Z
From role: implementer
Actor: k
To role: reviewer
Kind: planning_response
Thread: true_stmt_iife

Responds to the exact incoming claim:

`CLAIMED-FROM-reviewer-TO-implementer-2026-08-05T09-48-52Z-5e8b2b275fe7-BY-k-SEED-46dae92ab18cc9f2a91412ac92fee356-AT-2026-08-05T09-48-52Z`
Planning response for finding-true-statement-throwing-iife.  Static-only per
the gate (no in-tree edits, no probes, no compiles run).  Verdict: the
one-file test-only plan is SUFFICIENT as scoped; no assumptions disproved;
no additional existing-test edits beyond the PLAN.md ledger.  Details and
two small refinements below.

Static revalidation on the current tree (post the causal-unknown slice):

1. The route contract is intact and better documented than the 2026-08-03
   snapshot: `_visit_stmt_HExprStmt` excludes lambda callees from BOTH fast
   paths — HCall at hir_to_mir.py ~9090 (`not isinstance(stmt.expr.fn,
   H.HLambda)`, with a comment stating exactly the finding's rationale:
   indirect path ICEs on a raw HLambda and would double-wrap throw checking)
   and HInvoke at ~9105 (`not isinstance(stmt.expr.callee, H.HLambda)`).
   The generic tail's `lower_expr` dispatches HCall(fn=HLambda) to
   `_lower_lambda_immediate_call` (~5180).  FINDING.md's five-step route
   holds verbatim.
2. The misnamed test is exactly as described: the IIFE is the value operand
   of a try EXPRESSION (`val x = try (...)() catch { 7 }`), not an
   HExprStmt.  A repo-wide sweep confirms no OTHER test file claims
   statement-position coverage of the throwing shape — the PLAN's one-file
   ledger is complete; nothing further needs Slawomir approval.
3. Slice-interaction analysis (the tree moved a lot since the 2026-08-03
   baseline): the causal-suppression and pending-lambda consumer changes
   CANNOT touch this route — an IIFE callee is a literal HLambda, never a
   pending-registered binding (registration is HLet-only), and the swapped
   consumers key on HVar callees.  The assoc-call wrapper and
   method-receiver changes are likewise out of the shapes involved.
4. RISK FLAG for step 1's repro re-run: the repro lambda is a THROW-ONLY
   body with an explicit `-> Int` return — the exact shape of the standing
   "throw-only lambda w/ explicit return type" codegen follow-up note, and
   also a shape the 0.35.0 divergent-body work (spec-return authority +
   Unreachable finalize) materially changed — in the direction that should
   make it MORE robust, and the value-position twin passed in the aborted
   full run's driver stage.  I expect green, but if the re-run is red, PLAN
   step 6 (reclassify LANGUAGE_BUG, regression-first) is the right fork and
   the throw-only-lambda follow-up note is the first suspect to check.

Two refinements to the proposed test (both inside the already-approved
one-file scope):

A. The new statement-position test should also assert
   `"No MIR lowering" not in r.stderr` — the file's empty-IIFE test already
   pins that string, and it is the precise ICE signature of a mis-route to
   the indirect path, making the oracle sharper than exit codes alone.
B. Keep the repro's `return 99` after the throwing IIFE exactly as written:
   it keeps `fire()`'s body NON-divergent, so the test stays a pure
   statement-route pin with no coupling to the 0.35.0 divergent-finalize
   contract.  (I agree with dropping the all-terminal variant for the same
   reason — it would drag the divergence authority into this pin.)

Optional (reviewer's call): given the repro shape is a divergent lambda
body, adding `lang/tests/driver/test_stored_capturing_lambda_diagnostic.py`
(which carries the divergent-lambda companion pins) as a fifth focused gate
is cheap insurance; the four listed gates are otherwise well-chosen.

I also agree with the rename target (`test_throwing_iife_value_in_try_runs`)
and with keeping that test's source/oracle unchanged — its comment should
retain the "no double-wrapped throw checking" claim but scoped to the VALUE
route it actually exercises.

Standing by: no implementation until the run_all_tests.sh gate completes
cleanly and Slawomir's approval for the ledger is recorded.
