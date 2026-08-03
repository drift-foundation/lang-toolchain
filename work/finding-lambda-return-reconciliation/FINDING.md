# Finding: inferred lambda returns are not reconciled

Date: 2026-08-03

Classification: `LANGUAGE_BUG`

Suspected subsystem: `lang/driftc/type_checker.py`, specifically the first-pass
`HLambda` body-result authority and `type_stmt(HReturn)`.

## Minimal surface repro

See `repro_mixed_prefix_return_tail.drift`:

```drift
val f = | b: Bool | => {
	if b { return "x"; }
	1
};
```

The inferred signature is currently selected from the trailing `Int`, while the
earlier `String` return is typed with `return_type=None` and is never reconciled
against that selection.

## What the current tree does

There are two materially different observations because K's in-flight #1 patch
has strengthened the later hidden-lambda check:

1. The first-pass `TypeChecker` still accepts the mixed body.  The executable
   boundary tests in `red_first_pass_reconciliation.py` construct the HIR
   directly.  On the current tree both tests fail because the diagnostic list is
   empty; the direct call is recorded as `Int`.
2. A full driver compile of `repro_mixed_prefix_return_tail.drift` now exits 1
   with one `String`-versus-`Int` return diagnostic.  That is a downstream safety
   net, not closure of this finding: `driftc.py` re-checks the synthesized lambda
   as a standalone function using the already inferred `LambdaFnSpec.return_type
   == Int`, at which point K's shared `_type_return_value` authority catches the
   earlier return.

Consequently, a driver-only negative is not a regression-first test for this
bug: it is already green for the wrong architectural reason.  The mandatory red
test must call the primary `TypeChecker.check_function` authority once and
assert that it reports the conflict there.

## Static root cause

Current flow around `type_checker.py:7719-7881`:

1. An unannotated/uncontextual lambda sets the enclosing `return_type` cell to
   `None`.
2. `_lambda_body_result` sends every prefix statement through `type_stmt`.
3. `type_stmt(HReturn)` calls `_type_return_value(value, None, ...)`; this types
   and records the value, but has no expected type to compare against.
4. A trailing value is then typed once and selected as `body_result_ty`.
5. `lambda_ret_type` is assigned only from that result.  Earlier returns are not
   revisited or compared.

For statement-only bodies, `_find_return_expr` traverses the block and returns
only the first valued return.  Its recorded type becomes the signature, and all
later returns are ignored for inference compatibility.

`_find_return_expr` is therefore the remaining wrong abstraction.  A syntax
walk after typing cannot represent all exits, and looking up one expression
cannot reconcile them.  It should be replaced by observations captured at the
point each `HReturn` is originally typed.

## Constraints established by the surrounding patch

- Do not call `type_expr` on any return expression a second time.
- Keep K's `_type_return_value` as the single typing/coercion authority.
- Preserve the trailing value as the inferred candidate for value-blocks.
- Preserve the first valued explicit return as the candidate for statement-only
  bodies; this patch adds validation, not a new least-upper-bound algorithm.
- A nested `HLambda` is a function boundary.  Its returns must be collected by
  its own collector, never by the outer lambda.
- Suppress comparisons involving `Unknown` to avoid cascades.
- Do not apply a new late coercion after inference.  An annotation/context that
  supplies an expected return continues to get `_type_return_value`'s complete,
  lowering-visible coercion path during the original visit.  A fully inferred
  lambda whose observed return types differ should be rejected.

## Refactor-trigger and announcement checks

`doc/refactor_triggers.md` was scanned before designing the fix.  No registered
trigger covers inferred lambda result reconciliation; this remains a focused
compiler fix.  In particular, the registered ownership/borrow traversal
triggers do not fire for a type-result collector local to lambda typing.

`/tmp/drift-announce` did not exist at investigation time, so there were no
cross-team announcements to consume.  This research creates no release-worthy
change and publishes none.
