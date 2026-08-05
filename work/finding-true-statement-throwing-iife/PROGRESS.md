# PROGRESS: true statement-position throwing IIFE (implementer-owned)

Actor: K (implementer).  Start authorized by review-2026-08-05T10-46-02Z
(thread e593c2ca51f6) after the human memcheck full run completed cleanly.

## Step 1 — repro re-run (PLAN step 1): GREEN

Command (2026-08-05, current tree):

```
PYTHONPATH=. ./.venv/bin/python3 -m lang.driftc.driftc \
  work/finding-true-statement-throwing-iife/repro_true_statement_throwing_iife.drift \
  --entry repro::main --target-word-bits 64 --stdlib-root stdlib -o <scratch>/iife_repro
<scratch>/iife_repro ; echo $?
```

Result: compile + link succeeded; binary exit **0**.  Classification stays
regression-coverage debt (test-only); the LANGUAGE_BUG fork (PLAN step 6)
is NOT taken.

## Step 2 — approved one-file test edit: DONE

- `test_throwing_iife_statement_in_try_runs` RENAMED to
  `test_throwing_iife_value_in_try_runs`; comment now states the IIFE is
  the VALUE operand of the try expression, with the no-double-wrap claim
  scoped to the value route and a pointer to the statement twin.  Source
  and runtime oracle byte-identical.
- NEW `test_throwing_iife_true_statement_position_runs`: the repro
  fixture verbatim (throwing IIFE as discarded HExprStmt in `fire()`,
  `return 99` keeps the body non-divergent; main catches and returns
  `x - 7`).  Asserts compile success, `"No MIR lowering"` absent from
  stderr, binary exit 0.
- No other file touched; no compiler/spec/stdlib/history/version edits.

## Step 3 — five focused gates: DONE, ALL GREEN

`pytest -n8` over test_stmt_position_iife.py,
test_try_expr_immediate_lambda.py,
test_throws_terminal_body_flow_phase2.py, stage2/test_callinfo_cutover.py,
test_stored_capturing_lambda_diagnostic.py: **65 passed** (2m44s).

## Status: IMPL COMPLETE — handed off via baton (thread e593c2ca51f6).
Full suite / certification remain Slawomir-owned.
