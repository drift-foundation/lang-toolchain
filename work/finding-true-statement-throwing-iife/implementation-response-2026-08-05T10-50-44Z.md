# Baton message

Timestamp: 2026-08-05T10-50-44Z
From role: implementer
Actor: k
To role: reviewer
Kind: implementation
Thread: e593c2ca51f6

Responds to the exact incoming claim:

`CLAIMED-FROM-reviewer-TO-implementer-2026-08-05T10-46-02Z-5e50778fde05-BY-k-SEED-46dae92ab18cc9f2a91412ac92fee356-AT-2026-08-05T10-46-02Z`
finding-true-statement-throwing-iife implemented per the mutually reviewed
PLAN — test-only, one file, all five focused gates green (65 passed).

Step 1 (repro first): compiled/linked/ran
repro_true_statement_throwing_iife.drift on the current tree — exit 0
(exact command recorded in PROGRESS.md).  Classification stays coverage
debt; the LANGUAGE_BUG fork was not taken and no compiler code changed.
The throw-only-lambda risk flag from my planning response did not
materialize.

Step 2 (approved ledger, exactly one file —
lang/tests/driver/test_stmt_position_iife.py):
- RENAMED test_throwing_iife_statement_in_try_runs →
  test_throwing_iife_value_in_try_runs; the comment now says the IIFE is
  the VALUE operand of the try expression, scopes the no-double-wrap claim
  to the value route, and points at the statement twin.  Fixture source and
  runtime oracle are byte-identical.
- ADDED test_throwing_iife_true_statement_position_runs: the finding repro
  verbatim (`fire()` holds the throwing IIFE as a discarded HExprStmt,
  `return 99` keeps the body non-divergent; main catches and returns
  x - 7).  Asserts compile success, absence of "No MIR lowering" in
  compiler stderr (the ICE signature of a mis-route to the indirect path),
  and binary exit 0.

Step 3 (verification): the five PLAN gates in one focused run —
test_stmt_position_iife.py, test_try_expr_immediate_lambda.py,
test_throws_terminal_body_flow_phase2.py, stage2/test_callinfo_cutover.py,
test_stored_capturing_lambda_diagnostic.py — 65 passed, no failures.

run_all_tests.sh not touched; full suite and certification remain with
Slawomir.  PROGRESS.md carries the step-by-step record.
