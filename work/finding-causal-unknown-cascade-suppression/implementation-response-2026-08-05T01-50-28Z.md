# Implementation handoff

Timestamp: 2026-08-05T01-50-28Z
Role: implementer
Actor: k

Responds to the exact incoming claim:

`CLAIMED--REVIEW-PENDING-2026-08-05T01-49-32Z--BY-k--SEED-46dae92ab18cc9f2a91412ac92fee356--AT-2026-08-05T01-49-32Z`
# Planning round 3: revised plan and test-edit ledger VALIDATED (K)

Author: K (implementer), actor `k`, seed `46dae92ab18cc9f2a91412ac92fee356`

Responds to the claim recorded in the baton receipt for
`REVIEW-PENDING-2026-08-05T01-49-32Z` (target sha256
257e2ec47dd009dbfe0c8724f5474adca3498bf4db0f3b226e6d90c12dc27843).

## Test-edit ledger: ACCEPTED (my "none" conclusion corrected)

Verified against the current files: the cited prose exists exactly as
the ledger characterizes it —
- test_implicit_callback_wrap.py:45 "Site 1 ... NOT WRAPPED" narrative,
  plus the Site-2/5/6 "iface_coercion fallback / broken without the
  wrap" docstrings (112, 213, 258) that become historical-regression
  descriptions once the wrap is restored;
- test_stored_capturing_lambda_diagnostic.py:19 "suppressed when a prior
  error already explains the poisoned type" — the function-global
  wording the causal patch falsifies.
I agree both are same-slice mandatory under the stale-comment
guardrails; the arity-mismatch note stays conditional on the red/green
audit as written.  The ledger awaits Slawomir's approval; no other
existing-file edit is planned, contingencies stay gated.

## Five revised-plan points: ALL CONFIRMED

1. Expression provenance set {HVar read, HMove subject, causally
   suppressed call results, reachability-aware ternary join} matches the
   probe evidence.  One implementation note: the ternary join's
   reachability decision should reuse the shared `stage1.hir_flow`
   literal-folding authority rather than a local fold, so the provenance
   join and the checker's flow analysis cannot drift apart.  The
   "every reachable Unknown-producing arm caused" rule is the correct
   fail-toward-tripwire join.
2. CONFIRMED, and this ordering is the crux of the wrap restoration:
   constructing the callbackN wrapper FIRST and typing the inner HLambda
   through that context preserves capture-capable Callback literals
   (the allow_capture_invoke surface) — it mirrors the intact
   argument-position `_try_wrap_arg_for_callback_field` machinery, so
   the typed-let/return routes converge on one construction shape.
3. CONFIRMED — pending bindings stay thin Fn; only consumers own
   synthesized wrappers; matches the round-1/2 evidence and the
   Unknown-ABI-spec preservation requirement.
4. CONFIRMED — `&named_function` as a nested child with structural red
   proof first; my AttributeError capture is the seed evidence, and the
   HPlaceExpr-base hypothesis stays falsifiable until the walker trace
   exists.
5. CONFIRMED — four separate regression files (causal provenance,
   pending value finalization, callback slot materialization, fnptr
   borrow materialization); I withdraw my earlier three-file grouping.

No disagreement remains.  Awaiting: (a) full-suite gate, (b) Slawomir's
approval of the two-comment ledger, (c) terminal planning acceptance —
then implementation begins per the revised plan.
