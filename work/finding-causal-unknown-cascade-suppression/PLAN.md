# Plan: causally scoped Unknown cascade suppression

This is a proposed execution order, not a claim that the reviewer's patch
theory is correct. Reproduce and inspect first.

1. Re-read this entire finding and scan `doc/refactor_triggers.md`.
2. Run the work-only probe and confirm both intended independent-diagnostic
   assertions fail on the pre-fix tree for the claimed reason.
3. Re-run the existing stored-capturing-lambda driver pin and record its current
   one-primary-diagnostic behavior as the positive suppression baseline.
4. Trace every assignment of `Unknown` to a local binding, especially deferred
   stored-lambda resolution and final flushing. Separate diagnosed poison from
   undiagnosed invariant leaks.
5. Choose and document the smallest causal identity that covers the real
   producers. If using a `FnCheckState` table, include it in transaction
   ownership and rollback fingerprints.
6. Add the failing in-tree regressions before the root-cause fix:
   - unrelated error + independent `Unknown` copy use;
   - unrelated error + independent `Unknown` callee;
   - same poisoned stored-lambda binding still suppresses both cascades.
7. Implement the root-cause fix in the checker and call-resolver boundary.
8. Add shadowing and transaction rollback pins if the chosen representation can
   leak across either boundary.
9. Run focused checker/call-resolver/stored-lambda tests, then the appropriate
   broader gates after review convergence.
10. Record evidence, disagreements with this research, version/ABI decisions,
    and readiness for review in implementer-owned `PROGRESS.md`; then create the
    timestamped `IMPL-PENDING-*` token.

Suggested focused commands (revalidate paths before use):

```sh
./.venv/bin/python3 -m pytest -q work/finding-causal-unknown-cascade-suppression/probe_causal_unknown_suppression.py
./.venv/bin/python3 -m pytest -q lang/tests/type_checker/test_type_checker_copy_unknown.py
./.venv/bin/python3 -m pytest -q lang/tests/driver/test_stored_capturing_lambda_diagnostic.py
```
