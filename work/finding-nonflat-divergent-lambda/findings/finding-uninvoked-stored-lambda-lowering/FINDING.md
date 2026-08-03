# Finding: uninvoked unannotated stored lambda inside a lambda body has no MIR lowering

Parent: work/finding-nonflat-divergent-lambda (discovered while probing the
nested-lambda throw-effect boundary, 2026-08-03).

Classification: LANGUAGE_BUG (pre-existing, previously MASKED)

## Repro

`repro_uninvoked_stored_lambda.drift`:

```drift
val outer = | | nothrow => {
    val t = || -> Int => { throw ExcA(kind = 1); };
    0
};
val x = outer();
```

Current tree: checker accepts (correct — constructing a throwing lambda does
not throw), then MIR lowering ICEs:
`NotImplementedError: No MIR lowering for expr HLambda` while lowering the
hidden body's `val t = <lambda>` binding.

## Why it was invisible before

The old throw-effect walkers recursed into UNINVOKED nested lambda bodies, so
`outer` was misclassified may-throw and rejected "declared nothrow but may
throw" before lowering ever saw the binding.  The parent finding's boundary
fix (constructing ≠ executing) exposes this pre-existing lowering gap.

Suspected shape: an unannotated stored lambda that is never invoked gets no
pending-lambda resolution (that fires at the first CALL) and no LambdaFnSpec /
fnptr-const route, so `HLet.value` stays a raw `HLambda` when the hidden body
lowers.  Named-fn bodies likely hit the same gap (untested).

## Annotated variant is ALSO broken (second repro shape)

`val t: Fn() -> Int = || -> Int => { throw ExcA(kind = 1); };` inside a
lambda body gets further — the checker routes it through the fat-fn-ptr
path — but dies at clang: the nested hidden symbol
`repro::__lambda_fn_repro___lambda_fn_repro_main_3_3` is referenced by the
`%DriftFatFnPtr` insertvalue and never emitted (nested LambdaFnSpec inside a
hidden-lambda recheck is not lowered).  So EVERY stored-uninvoked-lambda
shape inside a lambda body is broken; only the lambda-as-direct-IIFE and
callback-wrapped routes work.

## Status

QUEUED — not started (parent finding in progress; serial rule).  Because
both driver-level shapes are blocked by THIS bug, the parent pins its
nested-lambda throw-effect boundary at the UNIT level (shared-walker test
over synthetic HIR) plus the driver-level NEGATIVE (IIFE inside nothrow
lambda rejects); the driver-level boundary POSITIVE lands with this fix.
