# Plan: reconcile every inferred-lambda return

This is a handoff proposal only.  No compiler or in-tree test file has been
changed here because K is concurrently finishing #1 in the same code.  Apply the
steps below after that work settles and refresh line numbers against the final
diff.

## 1. Install and confirm the red boundary regression

Move/adapt `red_first_pass_reconciliation.py` into a new file, preferably:

`lang/tests/type_checker/test_inferred_lambda_return_reconciliation.py`

Do not hide this in a driver-only test.  On the current tree the two direct
first-pass tests fail because no mismatch diagnostic is produced.  Confirm that
red state before changing `type_checker.py`.

Keep a driver negative too, but treat it as a clean-diagnostic/dedup pin.  K's #1
changes already make that test pass in the later hidden-lambda check, so it does
not prove the primary authority was fixed by itself.

## 2. Add a nested-lambda-safe return observation stack

Add a small private record near `LambdaFnSpec` (names illustrative):

```python
@dataclass(frozen=True)
class _LambdaReturnObservation:
	value: H.HExpr | None
	effective_type: TypeId
	span: Span
```

Inside `check_function`, add:

```python
lambda_return_observation_stack: list[list[_LambdaReturnObservation]] = []
```

Every `type_expr(HLambda)` pushes a fresh collector immediately before typing
its body and pops that exact collector in `finally`.  Because `type_stmt` always
appends to the top collector, a nested lambda automatically owns its returns and
cannot poison its parent.

This stack is ephemeral control state, not a new post-check side table.  It must
not escape the `HLambda` visit or become a second lowering input.

## 3. Record each `HReturn` during its only typing pass

In `type_stmt(HReturn)`, retain the result of the existing shared authority:

```python
effective_ty = self._void
if stmt.value is not None:
	...
	effective_ty = _type_return_value(...)
if lambda_return_observation_stack:
	lambda_return_observation_stack[-1].append(
		_LambdaReturnObservation(
			value=stmt.value,       # post-rewrite slot
			effective_type=effective_ty,
			span=getattr(stmt, "loc", Span()),
		)
	)
```

Important details:

- Capture `stmt.value` after `_type_return_value`, because auto-try, deref, or a
  callback wrapper may have replaced the slot.
- Capture the returned effective type, not a fresh `expr_types` lookup later.
- Do not duplicate coercion side tables in the observation.  The effective type
  is the post-coercion result, while the rewritten HIR slot and existing
  `iface_coercions`/CallInfo marks remain the lowering authorities.
- Record bare `return;` as `Void`; it must conflict with an inferred non-Void
  result, while an all-value-less body still infers `Void`.
- Named functions and standalone hidden-lambda re-checks see an empty stack and
  retain their existing behavior.

## 4. Delete `_find_return_expr` and select from observations

Pass the current collector into `_lambda_body_result` or close over it.

Candidate selection remains deliberately deterministic and compatible with the
existing surface contract:

1. Body expression or value-producing block tail: its one-pass
   `_type_return_value` result.
2. No value tail: the first observation with `value is not None`.
3. No valued return: `self._void`.

The statement-form match path therefore just calls `type_stmt(_last)` and then
reads the collector.  There is no HIR traversal and no return-expression
retyping after arm scopes have popped.

## 5. Reconcile only genuinely inferred lambdas

Remember whether the lambda entered body typing without an annotation or a
concrete contextual return, for example:

```python
infer_return_from_body = lambda_ret_type is None
```

After the body is typed and the candidate selected, if
`infer_return_from_body`:

1. Set `lambda_ret_type` to the candidate.
2. Compare every observation's `effective_type` with the candidate.
3. Skip if either type is `Unknown`.
4. Accept exact TypeId equality or normalized type-key equality.  Extract the
   `_same_type` logic currently nested in `_type_return_value` into a shared
   helper so aliases/FORWARD_NOMINAL placeholders behave identically.
5. Diagnose every incompatible observed return at its own return span.  Use a
   stable code, recommended `E-LAMBDA-INFERRED-RETURN-MISMATCH`, and a message
   such as:

   `return type 'String' does not match inferred lambda return type 'Int'`

Do not call `_type_return_value` during reconciliation: that would type the
expression twice and revive P1.1.  Do not silently install interface, callback,
`&T -> T`, or numeric-literal coercions after the fact unless a separate design
records every required HIR rewrite/side-table mark without revisiting the value.
The minimal sound contract is normalized-type agreement for a fully inferred
lambda.  Annotated/contextual lambdas already receive the full coercion contract
on the original pass.

Do not run the reconciliation for lambdas that entered with a known return:
`_type_return_value` already checks each return and tail against that return.
Running both would duplicate diagnostics.

## 6. Regression matrix

Add new tests; do not modify unrelated existing tests merely to accommodate the
patch.

Mandatory negatives:

1. First-pass boundary: prefix `return "x"` plus trailing `1`.
2. First-pass boundary: statement-only branches returning `1` and `"x"`.
3. Statement-form match with incompatible returns, including an arm-local
   binder return so the test proves the type was captured before scope pop.
4. Bare `return;` on one path plus a valued tail/return on another.
5. Driver compile of the minimal repro: one clean type-check diagnostic, no
   traceback, MIR-contract text, LLVM error, or duplicate hidden-pass message.

Mandatory positives, with full compile-and-run companions:

1. Prefix `return Int` plus trailing `Int`; call both paths and check the result.
2. Statement-only `Int` returns in both branches.
3. Statement-form match whose arm-local returns all agree.
4. Nested lambda returning `String` inside an outer lambda returning `Int`; the
   inner return must not enter the outer collector.
5. All value-less returns infer `Void` and invoke successfully.
6. Existing annotation/context coercion positives remain green; this collector
   must not alter K's `_type_return_value` marks.

Rerun at minimum:

```text
lang/tests/driver/test_lambda_return_inference_boundary.py
lang/tests/driver/test_lambda_return_inference.py
lang/tests/driver/test_drift_query_slice12_ices.py
lang/tests/driver/test_lambda_trailing_match_value.py
lang/tests/driver/test_try_expr_immediate_lambda.py
```

The first-pass tests guard the type-checker authority.  The compile/run tests
guard the TypeChecker -> CallInfo/LambdaFnSpec -> hidden lambda -> MIR boundary.

## Boundary/version assessment

This patch does not expand an accepted lowering shape and does not alter an ABI
signature/layout/calling convention.  Accepted programs retain the same
function return TypeId; inconsistent programs stop in the primary checker
instead of depending on a later re-check.  No ABI bump is indicated.

Per the user's explicit instruction, the compiler version has already been
bumped for the user-visible work in flight; do not add another version change
for this handoff.
