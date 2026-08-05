# Finding: throwing IIFE regression does not exercise statement position

Date filed: 2026-08-03

Origin: R5 static review of `finding-nonflat-divergent-lambda`.

Status: queued reviewer research. `PROGRESS.md` is implementer-owned and is
intentionally not created or edited by this research pass.

## Classification

**Confirmed test gap:**
`lang/tests/driver/test_stmt_position_iife.py::test_throwing_iife_statement_in_try_runs`
claims statement-position coverage, but its source is:

```drift
val x = try (|| -> Int => { throw MyExc(kind = 1); })() catch { 7 };
```

The IIFE is the value operand of `try`, not the expression of an `HExprStmt`.
It therefore exercises expression lowering and does not pin the modified
statement fast-path exclusion in `HIRToMIR._visit_stmt_HExprStmt`.

**Provisional classification:** regression-coverage/boundary-contract debt for
an already-fixed `LANGUAGE_BUG`, not a new compiler defect. If the true
statement-position probe fails on the current tree, reclassify that result as a
`LANGUAGE_BUG` and follow regression-first/root-cause policy before changing
the test expectation.

## Route that needs coverage

For source shaped as `(|| ... )();` in a block:

1. Stage1 produces `HExprStmt(expr=HCall(fn=HLambda, ...))`.
2. `HIRToMIR._visit_stmt_HExprStmt` must exclude the lambda callee from its
   ordinary direct-call fast path.
3. The generic statement tail calls `lower_expr(HCall)`.
4. Expression lowering dispatches to `_lower_lambda_immediate_call`, which owns
   the IIFE's throw checking/unwrapping.
5. On `Err`, the enclosing throwing function propagates. A `try/catch` at its
   caller observes the thrown error.

Routing the lambda through `_lower_indirect_call` is not equivalent: a raw
`HLambda` has no standalone MIR lowering, and wrapping the result again would
risk double throw checking.

## Minimal full compile/run reproducer

See `repro_true_statement_throwing_iife.drift`:

```drift
fn fire() -> Int {
	(|| -> Int => { throw MyExc(kind = 1); })();
	return 99;
}

pub fn main() nothrow -> Int {
	val x = try fire() catch { 7 };
	return x - 7;
}
```

The IIFE call itself is discarded in `fire`, so it is unambiguously an
`HExprStmt`. Runtime exit 0 proves its error escaped `fire` and reached the
caller's catch. Merely compiling is insufficient for this lowering-visible
route.

## Proposed test change

**Proposed:** preserve the existing value-position test under an accurate name,
then add the true statement-position compile/run case above. Do not silently
repurpose the old test unless its expression-position coverage is demonstrably
duplicated elsewhere.

A boundary-structure assertion is optional but useful: parse/lower far enough
to assert the `fire` body contains `HExprStmt(HCall(fn=HLambda))`. The source
shape is already direct, so the full compile/run test is the mandatory part.

## Acceptance criteria

- The new source compiles and links.
- Running it returns 0, proving the statement-position IIFE propagated and the
  outer catch observed the error.
- The source contains a genuinely discarded `HCall(fn=HLambda)` statement; the
  IIFE is not nested under `try`, assignment, return, or another value context.
- The existing empty, valueless, and discarded-owned-result statement IIFE
  pins remain green.
- The old value-position throwing test is renamed/reworded so it no longer
  claims to cover `_visit_stmt_HExprStmt`.

## Version/spec/ABI notes

- If the current implementation passes and the change is test/comment only,
  no compiler version or ABI bump follows from this finding.
- If the true pin exposes a source defect, apply the normal user-visible
  compiler-version rule after identifying the fix.
- No language-spec change is proposed or authorized.

## Refactor-trigger scan

**Observed:** `doc/refactor_triggers.md` was scanned on 2026-08-03. No entry
clearly matches this missing statement-route pin. Re-scan if the probe exposes a
current `LANGUAGE_BUG`.

## Open questions

- Does an existing phase-2 route test already assert the exact HIR statement
  shape? If so, retain one structural pin and avoid redundant introspection.
- Does the current tree pass the repro only because the outer function has an
  explicit `return 99` after the divergent statement? If so, add an all-terminal
  variant only if it exercises a distinct lowering path already in scope.

## Current-tree refresh (2026-08-05)

**Confirmed:** the lowering seam remains structurally unchanged on the pending
0.35.0 tree. `_visit_expr_HCall` routes a literal `HLambda` directly to
`_lower_lambda_immediate_call`; `_visit_stmt_HExprStmt` excludes literal lambda
callees from both ordinary call fast paths; and `_lower_indirect_call` retains
the assertion that a raw `HLambda` reaching it is a compiler bug. The proposed
source therefore still targets a live, deliberate boundary rather than dead
implementation history.

**Confirmed:** expression-position immediate-throw coverage is independently
present in `test_try_expr_immediate_lambda.py` and
`test_stored_capturing_lambda_diagnostic.py`. The existing test should still be
renamed accurately rather than deleted: it is a compact same-file parity guard,
but it is not the only expression-route pin.

**Decision:** do not add the all-terminal variant. The explicit `return 99`
deliberately isolates statement-IIFE error dispatch from the separate
terminal-flow/divergent-finalization authority. If the IIFE is incorrectly
discarded as nothrow, `fire()` returns 99 and `main()` exits 92 instead of 0;
the runtime oracle remains discriminating.

**Decision:** a separate structural parser/HIR assertion is not required for
this slice. The new test source visibly places `(HLambda)();` as its own
semicolon-terminated statement, and the mandatory compile/link/run outcome
tests the lowering behavior. Avoid adding a second parser/flattening harness to
the driver file unless implementation evidence shows the source no longer
lowers to `HExprStmt(HCall(fn=HLambda))`.

**Refactor-trigger result:** all entries in `doc/refactor_triggers.md` were
re-scanned on 2026-08-05. None matches a missing regression for the direct-IIFE
statement route. If the refreshed repro is red, reclassify it as a
`LANGUAGE_BUG`, but no registered larger refactor is currently triggered by
that shape.
