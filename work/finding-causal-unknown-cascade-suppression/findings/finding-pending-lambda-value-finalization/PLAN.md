# Plan: pending stored-lambda value finalization

This child is designed to be executed inside the parent causal-provenance
slice. Follow the parent's start gates and do not begin shared edits while the
preceding full suite is active. During that gate, K may add and run only narrow
compiler-invoking probes under this finding tree; shared tests and production
files remain untouched.

1. Re-run the existing pending alias/value-position probes and record ordered
   diagnostics.
2. Preserve the completed work-only probes for move, borrow, discarded HVar,
   compatible Callback argument, callback wrapper structure, capture effects,
   and a reference reached inside a deferred probe. Add only the remaining
   non-literal ternary/cause-join evidence requested by the parent.
3. Add new red in-tree tests for captureless alias compile/run, contextual
   Callback alias compile/run, source-order independence, clean unconstrained
   rejection, and clean capturing rejection.
4. Trace current HCall, HInvoke, HVar, and drain resolution side effects in
   exact order.
5. Extract a single finalization authority, entering through
   `PendingLambdaOwner.begin_resolution()` before every mutation.
6. Preserve thin-function binding type and lowering-visible Callback wrapping;
   reject Unknown ABI specs. Pending borrow finalizes and accepts; pending
   Callback argument finalizes first and wraps only in the consumer slot.
7. Mark or clear the parent's causal state as part of the same total outcome.
8. Replace duplicated HCall/HInvoke/drain logic only to the extent proven by
   the shared contract; do not add inference to call_resolver.
9. Run focused pending, stored-lambda, callback, transaction, and full
   compile/run tests.
10. Record whether the child folded cleanly or needs an independently scheduled
    continuation, then hand off through Baton.

No language-spec edit is proposed. Existing-test edits require Slawomir's
explicit approval; prefer new regression files.
