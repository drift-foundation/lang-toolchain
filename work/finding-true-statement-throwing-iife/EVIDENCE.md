# Evidence: true statement-position throwing IIFE

Snapshot date: 2026-08-03. Line numbers are navigational and may drift.

## Reproduced current-tree baseline

The work-only source was compiled with the repository driver and the produced
binary was executed.

**Observed on 2026-08-03:** compile/link succeeded and the binary exited 0.
Therefore the current implementation already handles the true statement route;
the open deliverable is an in-tree full compile/run regression and correction
of the misleading existing test name/comment. Revalidate after intervening
changes before treating this observation as current.

## Confirmed current test shape

`lang/tests/driver/test_stmt_position_iife.py:84-95` names its test
`test_throwing_iife_statement_in_try_runs`, but places the call in:

```drift
val x = try <IIFE>() catch { 7 };
```

This is a value-producing `try` expression. It does not make the IIFE itself an
`HExprStmt`.

## Confirmed changed lowering seam

At `lang/driftc/stage2/hir_to_mir.py:8970+`,
`_visit_stmt_HExprStmt` handles discarded expressions. Its ordinary `HCall`
and `HInvoke` fast paths explicitly exclude literal lambda callees. The nearby
comment states that direct statement IIFEs must fall through to generic
expression lowering because:

- `lower_expr(HLambda)` has no standalone lowering;
- `_lower_lambda_immediate_call` already checks/unwraps throwing results;
- the indirect-call statement path would wrap throw checking again.

At `hir_to_mir.py:10821+`, `_lower_indirect_call` has a labeled assertion if an
`HLambda` callee reaches it. This is a useful tripwire, not positive coverage of
the intended route.

## Why runtime observation matters

A compile-only pass could miss incorrect error dispatch, result wrapping, or
catch routing. The proposed program returns `x - 7`; exit 0 is possible only if
the thrown `MyExc` propagates out of `fire` and the outer `catch` selects 7.

## Suspected affected-file boundary

If the repro is green, this should touch only:

- `lang/tests/driver/test_stmt_position_iife.py` (or a dedicated neighboring
  driver test), including accurate route comments.

If it is red, the likely starting seam is
`lang/driftc/stage2/hir_to_mir.py::_visit_stmt_HExprStmt` and
`_lower_lambda_immediate_call`; do not assume that diagnosis without inspecting
the actual failure.
