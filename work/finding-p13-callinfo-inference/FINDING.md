# Finding: P1.3 tests contextual typing, while its source change is unreachable

Date: 2026-08-03

Classification: patch defect / dead code / boundary-test gap.  This is **not a
confirmed LANGUAGE_BUG** on the current tree: the live paths already infer and
record the lambda return correctly.  If the proposed characterization tests
turn red after #1/#2 settle, reclassify that failure immediately under the
repository's language-bug policy.

Suspected subsystem: lambda calls in `lang/driftc/checker/call_resolver.py` and
`lang/driftc/type_checker.py`, plus the P1.3 claims in
`lang/tests/driver/test_lambda_return_inference_boundary.py`.

## Finding 1: the two claimed inference tests inject `Int`

The existing tests use these shapes:

```drift
val r: Int = (|| => { ...; 6 })();
```

and:

```drift
val f = || => { ...; 6 };
val r: Int = f();
```

Both calls are initializer expressions for a binding declared `Int`.
`type_stmt(HLet)` passes that declared type as `expected_type` when typing the
initializer.

For the direct IIFE, the live `HCall(fn=HLambda)` resolver uses that expected
type as the lambda function's return slot before typing the lambda body.  For the
stored pending lambda, the `HCall(fn=HVar)` pre-resolution path likewise builds
the pending lambda's expected function type with `fn_ret=expected_type`.

Therefore neither test asks the lambda body to infer its return.  They are valid
contextual-typing tests, but they would remain green if the no-context inference
route regressed to `Unknown`.

## Finding 2: K's P1.3 source addition is in a dead duplicate branch

`resolve_call_expr` contains two `if isinstance(expr.fn, H.HLambda)` branches:

1. The live branch starts near `call_resolver.py:5100`.  It:
   - types arguments;
   - constructs a function expectation whose return is `expected_type` or
     `Unknown`;
   - calls `type_expr(expr.fn, expected_type=callee_expected)`;
   - extracts `fn_sig_ret` from the resulting function TypeId;
   - records that exact return in CallInfo; and
   - returns on every control-flow path.
2. The duplicate starts near `call_resolver.py:6019`.  No
   `HCall(fn=HLambda)` can reach it because the first branch has already
   returned.  K's new `type_expr(lam)`/function-return extraction was added
   inside this unreachable block.

The correct source patch is not to transplant those new lines into the first
branch: the first branch already performs the intended operation.  Delete the
entire second lambda-call branch, including the in-flight addition.

## Finding 3: stored source calls are not `HInvoke`

Stage 1 deliberately emits:

- direct lambda literal invocation as `HCall(fn=HLambda)`;
- a call whose callee is a name as `HCall(fn=HVar)`; and
- `HInvoke` only for a computed callable expression that is not a bare name,
  qualified member, or lambda literal.

The included parser probe confirms that:

```drift
val f = || => { 7 };
val stored = f();
```

contains `HCall(fn=HVar)`, not `HInvoke`.  Its CallInfo target is nevertheless
`INDIRECT`, which is the semantic fact lowering needs.

`type_checker.py` does have a supported `HInvoke(callee=HLambda)` path.  A
synthetic-HIR boundary probe confirms it also infers `Int` and records that in
CallInfo, but the existing stored-lambda source test does not exercise it.

## Empirical evidence

`probe_callinfo_inference.py` contains four no-context checks:

1. live direct `HCall(fn=HLambda)` CallInfo;
2. actual stored-source shape `HCall(fn=HVar)` with indirect CallInfo;
3. synthetic `HInvoke(callee=HLambda)` CallInfo; and
4. parser node-shape assertions for direct and stored source calls.

Current result:

```text
4 passed in 0.54s
```

All three call probes record `CallInfo.sig.user_ret_type == Int`, an `INDIRECT`
target, and `expr_types[call.node_id] == Int`, without a declared result type or
other expected return context.

`repro_no_expected_result.drift` also compiles, links, and runs with exit 0.  Its
direct and stored call results are first bound without annotations; only later
arithmetic consumes them.

A `python -m trace --count --missing` run over the probes recorded execution in
the live 5100 branch and the live `HInvoke` branch.  At the 6019 duplicate, only
the `if` condition was evaluated for a non-lambda stored `HCall(fn=HVar)`; every
line in the branch body, including the new inference block, was marked
unexecuted.

## Trigger and announcement checks

`doc/refactor_triggers.md` was scanned.  No registered trigger concerns duplicate
call-resolver branches or lambda CallInfo inference, so no larger refactor is
mandated.  Removing the directly implicated dead branch is sufficient.

`/tmp/drift-announce` did not exist at investigation time, so there were no
cross-team announcements to consume.  This research makes no release change and
publishes none.

