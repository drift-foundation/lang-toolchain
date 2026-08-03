# PLAN: fix value-block lambda return-type inference (0.34.2)

Per reviewer direction (3 P1s + related gap + gate order).

## P1.1 — one checker authority (no double-typing)
The lambda tail was typed twice: `type_block()` types every `HExprStmt` with
`used_as_value=False`, then the inference re-typed the tail as a value —
duplicating diagnostics / copy-move accounting / auto-try / call resolution /
`expr_types`.

Fix structurally: type the lambda block in ONE scope — prefix statements
normally, the final value-producing expression **exactly once** in value
context. The authority returns the inferred body-result type. Apply to both the
`body_block` and `body_expr` paths.

## P1.2 — authoritative statement_form (not a heuristic)
The parser's `statement_form` bit is authoritative but was not retained on
`HMatchExpr`. A heuristic over arm terminators misclassifies nested terminal
control flow (an arm ending in `HIf` that returns in both branches) → reintroduces
`E-MATCH-NO-VALUE`.

Fix: carry `statement_form` into `HMatchExpr`, set it in `_lower_match_expr`, and
preserve it through every HIR reconstruction site (alpha-renamer,
`borrow_materialize`, `place_canonicalize`). The authority consults the flag; do
NOT infer source form from arm terminators.

## P1.3 — direct HCall(fn=HLambda) CallInfo
The direct-call resolver (`checker/call_resolver.py:6019`) does not type the
lambda body; with no surrounding expected type it records
`call_ret = lambda_ret_type or ctx.unknown_ty`, so the primary typed boundary is
`Unknown` (a later checker path independently lets the program compile).

Fix: route BOTH direct `HCall(fn=HLambda)` and `HInvoke(callee=HLambda)` through
the same lambda-result authority and record the exact inferred return in
CallInfo. Add a **checker-boundary assertion for both routes** (not only a
downstream arithmetic use).

## Related gap — declared/expected comparison
When a declared/expected return exists, the inference block was skipped while the
tail was typed as a discarded statement → mismatches uncaught. The one-pass
authority must compare the actual tail type with the declared/expected type. Add
a negative block-tail pin (expected `Callback0<Int>` ending in a `String`);
preserve negatives for genuinely incompatible declared/expected returns.

## Tests
- Repurpose the stale Void-inference negative in
  `test_lambda_trailing_match_value.py` into a compile/run positive.
- Compile/run companions for **plain**, **match**, and **try** trailing values.
- Checker-boundary assertion (both call routes) = `Int`.
- Keep negative coverage (incompatible declared/expected; return-in-arm parse
  rejection).

## Version
`lang/versions.py`: 0.34.1 → **0.34.2**, ABI 22 unchanged.

## Gate order (do NOT run run-all-tests.sh before re-promotion)
run-all-tests.sh no longer runs the corpus (removed), but the ordering discipline
for the maintainer's manual corpus step stands:
1. `ownership-corpus-check --fresh`
2. review + `ownership-corpus-promote`
3. commit the golden baseline/fingerprint
4. run the full suite

Running the corpus verify before re-promotion would only fail against the stale
0.34.1 baseline.
