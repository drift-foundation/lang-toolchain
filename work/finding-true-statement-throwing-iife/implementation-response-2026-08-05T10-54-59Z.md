# Baton message

Timestamp: 2026-08-05T10-54-59Z
From role: implementer
Actor: k
To role: reviewer
Kind: implementation
Thread: e15f49176789

Responds to the exact incoming claim:

`CLAIMED-FROM-reviewer-TO-implementer-2026-08-05T10-49-05Z-1276b6fff548-BY-k-SEED-46dae92ab18cc9f2a91412ac92fee356-AT-2026-08-05T10-54-23Z`
Acknowledged — resume confirmed and already satisfied: the finding was
FINISHED per the locked plan before the pause/resume pair arrived
(implementation handoff on thread e593c2ca51f6: repro green, one-file
approved edit applied, five focused gates 65 passed).  Nothing was
broadened.

The preceding-slice commit message + exact staging exclusion list went out
in implementation-response-2026-08-05T10-54-17Z (with the amendment
removing the IIFE sentence from its Tests paragraph).  Here is the SEPARATE
proposed commit message for this test-only finding:

---8<---
test: true statement-position throwing IIFE gets its own compile/run pin

test_throwing_iife_statement_in_try_runs claimed statement-position
coverage, but its IIFE is the VALUE operand of a try expression — it
exercises expression lowering, not the statement fast-path exclusion in
HIRToMIR._visit_stmt_HExprStmt.  Rename it to
test_throwing_iife_value_in_try_runs with an accurate route comment
(source and runtime oracle unchanged), and add
test_throwing_iife_true_statement_position_runs: a throwing IIFE as a
genuinely discarded HExprStmt inside a throwing fn (followed by
`return 99` so the body stays non-divergent), whose caller observes the
propagated error through try/catch.  The new pin asserts compile
success, the absence of "No MIR lowering" (the ICE signature of a
mis-route to the indirect-call path, which cannot lower a raw HLambda
and would double-wrap throw checking), and binary exit 0.

Current behavior was verified green first (the finding repro compiles,
links, and runs exit 0), so this is regression-coverage debt repair for
the already-fixed 0.34.2 statement-IIFE LANGUAGE_BUG — test-only, no
compiler change.
---8<---

Scope of that commit: exactly lang/tests/driver/test_stmt_position_iife.py
(the work/finding-true-statement-throwing-iife/ artifacts stay uncommitted
per the usual work/ policy unless Slawomir wants them recorded).  Not
committing; both boundaries are ready for selective staging.
