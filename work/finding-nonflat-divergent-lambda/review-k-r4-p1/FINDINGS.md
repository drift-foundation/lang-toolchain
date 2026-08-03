# Static review findings: K's R4 / P1 patch

Date: 2026-08-03

Verdict: the shared return authority is materially improved, but the patch is
not ready to close.  Three P1 findings remain in the submitted scope, followed
by two P2 hardening/test issues.

## P1 — the non-flat divergent-lambda negative semantically masks a known lowering bug

`type_checker.py:7836-7865` accepts only a literal final `HThrow`/`HRethrow` as a
non-Void lambda body with no value.  A semantically terminal body such as:

```drift
|| -> Int => {
	if cond { throw A(); } else { throw B(); }
}
```

is rejected as “must return a value” solely because `_last` is `HIf` rather
than `HThrow`.  The source comment explicitly says this is because codegen
mislowers the non-flat shapes.

`test_stored_capturing_lambda_diagnostic.py:78-94` then pins that clean
rejection as the contract.  That is not closure under the repository's language
bug and no-semantic-masking rules: a checker rejection is being used to hide a
known lowering defect, and the user explicitly ruled that pre-existing bugs
found in this work must be fixed.

Classify the non-flat form as its own `LANGUAGE_BUG`, add a failing compile/run
regression first, fix the hidden-lambda control-flow/finalization path, and turn
this negative into a positive.  Cover both branches and at least one nested
terminal block or statement-form match.  Once lowering handles terminal CFGs,
the return-value requirement must use semantic fallthrough/terminal analysis,
not the class of the final syntax node.

## P1 — the required lambda-tail coercion positive is still absent, and that route is broken

`test_lambda_return_inference_boundary.py:211-228` tests only named functions
whose producers are explicit `HReturn` statements.  It proves the named-return
half of `_type_return_value`; it does not exercise a lambda block trailing value
or the hidden callback boundary requested by the review.

The current hidden-function reconstruction treats equivalent lambda forms
differently at `driftc.py:6756-6762` and again at `7651-7658`:

- an expression body is wrapped in `HReturn` before the standalone re-check;
- a block tail remains `HExprStmt`, so ordinary function checking never applies
  the hidden function's expected interface return to it.

Lowering consumes only the regenerated `hidden_typed_fn.iface_coercions` at
`driftc.py:7317-7331`.  The original enclosing lambda's coercion mark therefore
does not reach `HIRToMIR.lower_expr` (`hir_to_mir.py:2783-2808`), and the hidden
function returns raw `Dog` while its signature says `Speaker`.

The completed reduction and expected-red MIR/full-driver probes are in:

`work/finding-lambda-tail-coercion-positive/`

The mandatory positive remains:

```drift
val cb: core.Callback0<Speaker> = core.callback0(|| => {
	Dog(n = 7)
});
```

It must compile/run and the hidden MIR must contain
`ConstructIfaceValue(iface_ty=Speaker, value_ty=Dog)`.  Use one shared
lambda-as-function-body normalizer for both reconstruction loops; do not copy
side tables across deep-copy/normalization boundaries.

## P1 — P1.3's source addition is unreachable and its claimed boundary tests inject the result type

The live `HCall(fn=HLambda)` branch starts at
`call_resolver.py:5100`, calls `type_expr(expr.fn, expected_type=...)`, extracts
the function return, records CallInfo, and returns at `5195-5196`.  Therefore no
lambda call can reach the duplicate branch at `6019`; the new `type_expr(lam)`
logic at `6041-6052` is dead code.

The two claimed inference tests also do not test no-context inference:

- `test_lambda_return_inference_boundary.py:64-75` puts the IIFE in
  `val r: Int = ...`, which supplies `Int` as the expected result before the
  lambda is typed;
- `:78-89` supplies the same context at the stored call and incorrectly calls
  that source producer `HInvoke`.  `ast_to_hir.py:444-453` emits a stored-name
  call as `HCall(fn=HVar)`; `HInvoke` is reserved for computed callees.

This remains exactly the researched P1.3 finding in:

`work/finding-p13-callinfo-inference/`

Delete the unreachable duplicate branch.  Add direct no-context CallInfo
assertions for the live `HCall(fn=HLambda)`, actual stored `HCall(fn=HVar)`, and
a separately labeled synthetic `HInvoke(callee=HLambda)`, plus the no-context
full compile/run companion.

## P2 — Unknown cascade suppression is function-global rather than causally scoped

Both new suppression gates use “there is any prior error anywhere in the
function”:

- `type_checker.py:3992-3998` suppresses `E-COPY-UNKNOWN`;
- `call_resolver.py:6749-6754` suppresses “call target is not a function value”.

That does not establish that the earlier error poisoned this expression or
binding.  An unrelated earlier diagnostic now disables the Unknown tripwire for
all later values/calls in the function, contrary to the comments' claim that a
prior error “already explains” the slot.

Track causal poison by node/binding ID, or snapshot the diagnostic state around
the producer and carry an explicit poisoned marker.  The existing single-error
test proves the desired presentation but cannot detect cross-statement
suppression; add a two-independent-errors test.

## P2 — the throwing “statement IIFE” test never takes the changed statement route

`test_stmt_position_iife.py:84-95` uses:

```drift
val x = try (|| -> Int => { throw ...; })() catch { 7 };
```

The IIFE is the operand of a value `try` expression, not an `HExprStmt`.  It
therefore uses `_visit_expr_HCall`, the route that already handled immediate
lambdas, and does not cover the new exclusion at
`hir_to_mir.py:9184-9199` or the claimed no-double-wrap statement behavior.

Add a genuinely discarded throwing IIFE statement inside a throwing function,
then call that function under `try/catch` from `main` and assert the caught
result.  This is the changed path that needs the compile/run pin.

## Contract-comment cleanup

Before closure, remove contradictory comments:

- `type_checker.py:12136-12138` still says return coercions omit the implements
  probe and remain unverified, while R4.1 adds that probe immediately below;
- `test_checker_call_type_checks.py:95-97` says body re-inference survives as a
  CallInfo-less fallback, while R4.2 deletes the fallback entirely.

The existing return-implements relation itself and the ordering of Unknown
suppression before interface processing look correct in this static pass.

## Known queued work

The separate inferred-return reconciliation finding remains open as expected:
`_find_return_expr` still selects only the first return and does not reconcile
all exits.  It is documented under `work/finding-lambda-return-reconciliation/`
and is not duplicated as a new finding here.

Static review only; no tests run.
