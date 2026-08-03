# Finding: the lambda-tail coercion positive is absent because the hidden boundary drops the mark

Date: 2026-08-03

Classification: `LANGUAGE_BUG`

Suspected subsystem: hidden-lambda function reconstruction in
`lang/driftc/driftc.py`, between the primary `HLambda` return authority in
`lang/driftc/type_checker.py` and `HIRToMIR`'s consumption of
`TypedFn.iface_coercions`.

## The requested pin is still absent

The review requested a positive shaped like:

```drift
val cb: core.Callback0<Speaker> = core.callback0(|| => {
	Dog(n = 7)
});
```

where `Dog implements Speaker`, with proof that the lowering-visible interface
coercion is recorded and consumed.

`lang/tests/driver/test_lambda_return_inference_boundary.py` currently has a
positive named `test_return_interface_coercion_positive_runs`, but both of its
producers are named-function `HReturn` statements (`return Dog(...)` and
`return move dog`).  It proves the shared named-return path; it does not execute
a lambda block's trailing-value producer or the hidden callback-function
boundary.  It therefore does not close the requested pin.

## Minimal repro and observed failure

See `repro_callback0_speaker_tail.drift`.  On the current tree, the valid
program fails compilation with:

```text
typecheck contract failure: SSA return type does not match declared signature
for repro::__lambda_cb_main_0_0 in entry (15 vs 16)
```

The TypeIds are the concrete `Dog` and interface `Speaker`.  This is an internal
boundary failure reaching the user, so it is independently a language bug under
the repository rules.

The executable red probe gives stronger evidence than the final SSA failure.
`compile_stubbed_funcs` produces hidden callback MIR whose instructions are:

```text
ConstString(...), ConstInt(...), ConstructStruct(... struct_ty=Dog ...)
```

There is no `M.ConstructIfaceValue`.  The hidden function's declared return is
nevertheless `Speaker`; SSA validation later catches that disagreement.

Run the two red boundaries explicitly with:

```bash
./.venv/bin/python3 -m pytest -q work/finding-lambda-tail-coercion-positive/red_hidden_lambda_coercion_positive.py
```

Expected before the fix: two failures.  The MIR test fails because the hidden
function has no `ConstructIfaceValue`; the driver test fails with the internal
SSA return-type diagnostic.  Expected after the fix: two passes and executable
exit 0.

## Sibling-form isolation

The included repros establish that this is specifically the block trailing
value reconstruction path:

| Producer | Current result |
|---|---|
| `Callback0<Speaker>` with `{ Dog(...) }` tail | fails, missing interface construction |
| same callback with `{ return Dog(...); }` | compiles and runs, exit 0 |
| same callback with expression body `=> Dog(...)` | compiles and runs, exit 0 |
| direct annotated IIFE with `{ Dog(...) }` tail | fails with the same `Dog`/`Speaker` SSA contract |
| non-implementing `Cat` block tail | one clean checker diagnostic |

Files:

- `repro_callback0_speaker_tail.drift`
- `repro_callback0_speaker_explicit_return.drift`
- `repro_callback0_speaker_expr_body.drift`
- `repro_iife_speaker_tail.drift`
- `repro_iife_speaker_expr_body.drift`
- `repro_callback0_nonimplementing_tail.drift`

The negative already exits 1 with exactly:

```text
'Cat' does not implement interface 'Speaker'
```

so the implements-relation check in the primary return authority is working.
The lost artifact is the positive lowering mark on the regenerated hidden body.

## Static root cause

The primary lambda check is correct at the source HIR boundary:

1. The expected callback return is `Speaker`.
2. `_lambda_body_result` sends the trailing `Dog(...)` through the shared
   `_type_return_value` authority.
3. That authority verifies `Dog implements Speaker` and calls
   `record_iface_coercion(value_expr, Speaker)`.
4. Ordinary source-function lowering receives that enclosing `TypedFn` side
   table.

Callback lowering then creates a `HiddenLambdaSpec` with
`return_type_id=Speaker` and the original `HLambda`.  In the hidden-lambda loop
at `driftc.py:6756-6762`, the lambda is deep-copied and rebuilt as a standalone
function for a second check:

```python
if lam.body_expr is not None:
	lambda_body = H.HBlock(statements=[H.HReturn(value=lam.body_expr)])
elif lam.body_block is not None:
	lambda_body = lam.body_block
```

This treats the two equivalent surface forms differently:

- an expression body becomes `HReturn`, so `check_function(return_type=Speaker)`
  invokes `_type_return_value` again and records the hidden body's mark;
- a block trailing value remains `HExprStmt`, so ordinary function checking
  types it only in discarded statement context and never reconciles it with the
  standalone function's `Speaker` return.

The lowering instance at `driftc.py:7317-7331` correctly consumes
`hidden_typed_fn.iface_coercions`, but that newly generated table has no entry
for the tail.  `_lower_lambda_block` therefore emits the raw `Dog` value and the
caller installs it as the hidden function return.

There is a parallel reconstruction at `driftc.py:7651-7658` for
`LambdaFnSpec`; it has the same expression-body/block-body asymmetry and should
use the same fix even if the callback repro reaches the earlier worklist.

`_hidden_lambda_ret_type` is a related authority leak.  It derives a hidden
signature from raw `expr_types` and ignores `iface_coercions`.  The callback
worklist avoids that helper while `spec.return_type_id` is concrete, but the
captureless worklist calls it unconditionally.  A hidden function already has
an authoritative return in its spec; a raw concrete tail must not overwrite an
expected/inferred interface result.

## Patch direction

Normalize every regenerated lambda into ordinary function-return HIR before its
standalone check:

```python
def _lambda_as_function_body(lam: H.HLambda) -> H.HBlock:
	if lam.body_expr is not None:
		return H.HBlock(statements=[H.HReturn(value=lam.body_expr, loc=lam.body_expr.loc)])
	if lam.body_block is None:
		raise AssertionError("lambda missing body (checker bug)")
	block = lam.body_block
	if block.statements:
		last = block.statements[-1]
		statement_match = isinstance(last, H.HExprStmt) and isinstance(last.expr, H.HMatchExpr) and last.expr.statement_form
		if isinstance(last, H.HExprStmt) and not statement_match:
			block.statements[-1] = H.HReturn(value=last.expr, loc=last.loc)
	return block
```

The exact helper name is not important; one helper must serve both reconstruction
loops.  Construct the `HReturn` before `normalize_hir` so node/callsite IDs and
later rewrites see the final function shape.  Preserve the trailing statement's
span.  Do not wrap a statement-form match: its internal explicit returns are
already the exits and its `HExprStmt` is intentionally non-value-producing.

This reuses the established `HReturn` check/lowering contract.  It does not
retype an expression inside the original lambda pass; the separate check is for
the independently synthesized hidden function and is already part of the
pipeline.

Also keep the spec return authoritative:

- hidden callback: retain the current `spec.return_type_id`, with inference only
  when it is genuinely `Unknown`;
- captureless `LambdaFnSpec`: start from `spec.return_type` and invoke fallback
  inference only when that type is `Unknown`;
- if `_hidden_lambda_ret_type` remains, have its effective expression lookup
  prefer `typed_fn.iface_coercions[node_id]` over raw `expr_types[node_id]`.

Do not copy the enclosing function's coercion side table into the hidden
`TypedFn`.  The lambda is deep-copied, normalized, and binding-remapped; side
tables belong to the HIR instance that produced them.  Rechecking the normalized
hidden function through the shared `HReturn` authority records a valid local
mark with the correct node identity.

## Refactor-trigger and announcement checks

`doc/refactor_triggers.md` was scanned before designing this patch.  No
registered trigger covers hidden-lambda return normalization or interface
coercion side-table transfer; this remains a focused boundary repair.

`/tmp/drift-announce` did not exist at investigation time, so there were no
cross-team announcements to consume.  This research itself changes no release
surface and publishes none.

## Additional symptoms of the same root (evidence dropped 2026-08-03, from finding-nonflat-divergent-lambda)

The `_hidden_lambda_ret_type` authority leak (raw tail-derived type overwrites
the spec's return) has two more driver-visible symptoms beyond Dog→Speaker,
both on STORED throwing lambdas (`LambdaFnSpec` worklist):

1. `repro_stored_throwing_value_match_void_ret.drift` — stored `|k| -> Int`
   whose tail is a value bool-match with a can-throw arm result: hidden fn
   emitted as `FnResult_Void_Error` (declared Int overwritten by Void derived
   from the statement-context tail); the match value is computed then
   DISCARDED — `f(4)` returns 0 instead of 5 (silent wrong answer).
2. `repro_stored_terminal_call_unknown_ret.drift` — stored `|n| -> Int` whose
   body is a terminal-`throws` tail call: hidden fn ok-type becomes UNKNOWN →
   "LLVM codegen v1: FnResult ok type UNKNOWN is not supported".
   (The direct-IIFE and named-fn variants of the same body RUN after the
   nonflat-divergent-lambda fixes — only this stored reconstruction breaks.)

Both should fall out of the planned normalizer + spec-return-authority fix.
