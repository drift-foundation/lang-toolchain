# Plan: true statement-position throwing IIFE regression

1. Re-read the finding and re-scan `doc/refactor_triggers.md` if the current
   repro fails.
2. Compile and run `repro_true_statement_throwing_iife.drift`; record whether
   the current tree is already green.
3. Inspect the produced HIR or add a focused structural assertion confirming
   `HExprStmt(HCall(fn=HLambda))` in `fire`.
4. Add a full driver compile/run regression using the repro source.
5. Rename or rewrite the existing value-position test/comment so it accurately
   describes its route; keep it if it protects non-duplicated behavior.
6. If the new regression is red, stop treating this as test-only debt:
   classify the observed failure as `LANGUAGE_BUG`, retain the failing test,
   diagnose the root cause, and fix the compiler rather than weakening source.
7. Run the focused statement-IIFE, hidden-lambda, try/catch, and throwing-call
   suites. No broad gate is needed until the serial implementation round is
   otherwise ready.
8. Record evidence and any corrections to this proposal in implementer-owned
   `PROGRESS.md`, then create the timestamped `IMPL-PENDING-*` token.

Suggested focused commands (revalidate paths before use):

```sh
./.venv/bin/python3 -m pytest -q lang/tests/driver/test_stmt_position_iife.py
./.venv/bin/python3 -m pytest -q lang/tests/driver/test_throws_terminal_body_flow_phase2.py
```
