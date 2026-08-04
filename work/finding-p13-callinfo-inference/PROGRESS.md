# Progress: P1.3 no-context CallInfo pins + dead branch deletion

Last updated: 2026-08-04 (K; folded into the active
finding-lambda-return-reconciliation slice per
review-2026-08-04T20-32-09Z).  Supersedes the 2026-08-03 research
checklist: its six open items are completed below; research evidence
(line-trace proof of unreachability etc.) remains valid history.

STATUS: IMPLEMENTED — gates shared with the reconciliation slice.

1. Probes re-verified 4/4 green against the reconciled collector BEFORE
   adaptation, then installed as
   lang/tests/type_checker/test_lambda_callinfo_inference_boundary.py:
   live direct `HCall(fn=HLambda)` no-context CallInfo (user_ret_type
   Int, INDIRECT, concrete expr type, non-Unknown lambda fn type);
   actual stored-source `HCall(fn=HVar)` shape with the indirect
   target's callee id pinned to the resolved binding id; the synthetic
   `HInvoke(callee=HLambda)` contract (labeled synthetic — ordinary
   stored syntax never emits it); the producer-shape parse pin (direct =
   HCall(fn=HLambda), stored = HCall(fn=HVar), no HInvoke); and the
   unannotated compile/run companion (exit 0).
2. The UNREACHABLE duplicate `HCall(fn=HLambda)` branch in
   checker/call_resolver.py (was ~:6019, including the in-flight
   inference addition) is DELETED outright — nothing transplanted into
   the live ~:5100 branch; a NOTE comment marks the position and points
   at the pin file.  Exactly one lambda-call authority remains in
   resolve_call_expr.  The type_checker.py HInvoke implementation is
   untouched per plan.
3. Slawomir-approved comment corrections applied in
   lang/tests/driver/test_lambda_return_inference_boundary.py: module
   prose + both annotated cases now describe CONTEXTUAL result
   propagation; stored source described as HCall(fn=HVar) with an
   INDIRECT CallInfo target (HInvoke claims removed); the R3.P1
   comment's route naming corrected.  No test source/assertion/
   expectation/behavior change.
4. Version: no bump (rides pending 0.35.0); ABI 22.  History: covered by
   the folded 0.35.0 entry paragraph.

Gates: shared combined battery + compiler-suite smoke with the
reconciliation slice (results in that finding's PROGRESS.md).
