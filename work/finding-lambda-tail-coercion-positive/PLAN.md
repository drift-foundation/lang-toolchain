# Plan: preserve lambda-tail coercions through hidden-function reconstruction

This is a handoff proposal only.  No compiler, runtime, stdlib, or in-tree test
file has been changed because K is concurrently finishing #1.  Refresh the diff
and line numbers before implementation.

## 1. Install the red regressions first

Adapt `red_hidden_lambda_coercion_positive.py` into a new in-tree stage2/driver
test file rather than weakening the existing named-return positive.

The first test is the required lowering-visible mark pin:

1. Parse the `Callback0<Speaker>`/`Dog` block-tail source with the real stdlib.
2. Call `compile_stubbed_funcs(..., return_checked=True)`.
3. Find the one `__lambda_cb_*` MIR function.
4. Assert its instructions include `M.ConstructIfaceValue` whose `iface_ty` is
   `Speaker` and whose `value_ty` is `Dog`.

On the current tree, the diagnostic-free MIR exists but contains only the raw
`ConstructStruct(Dog)`, so this assertion is red at the exact checker-to-lowering
boundary.

The second test must invoke the real driver, link, run, and assert exit 0.  It is
red today because SSA validation observes `Dog` returned from a function
declared `Speaker`.  This full compile/run companion is mandatory for a
lowering-visible acceptance fix.

## 2. Add one shared function-body normalizer

In `compile_stubbed_funcs`, add a private helper near the two hidden-lambda
worklists that converts a deep-copied `HLambda` into its standalone function
body:

- expression body -> one `HReturn`;
- block body ending in a value-producing `HExprStmt` -> replace that final
  statement with `HReturn` over the same expression;
- statement-form `HMatchExpr`, existing `HReturn`, empty/value-less bodies, and
  other statements -> retain their statement form.

Preserve `loc` and call `normalize_hir` after conversion.  Use this helper at
both current reconstruction sites around `driftc.py:6756` and `driftc.py:7651`.
Do not add interface-specific logic to `_lower_lambda_block`; the correct
`TypedFn.iface_coercions` mark should make its generic `lower_expr` path emit
`ConstructIfaceValue`.

## 3. Keep the hidden signature on the CallInfo/spec authority

The hidden callback worklist already starts with `spec.return_type_id` and falls
back only for `Unknown`; retain that contract.

Change the captureless worklist from unconditional raw-tail derivation:

```python
lambda_ret_type = _hidden_lambda_ret_type(lambda_body, lambda_typed_fn, shared_type_table)
```

to the same contract:

```python
lambda_ret_type = spec.return_type
if shared_type_table.get(lambda_ret_type).kind is TypeKind.UNKNOWN:
	lambda_ret_type = _hidden_lambda_ret_type(lambda_body, lambda_typed_fn, shared_type_table)
```

Harden the fallback so an expression-level interface coercion is its effective
type:

```python
coerced = typed_fn.iface_coercions.get(value.node_id)
if coerced is not None:
	return coerced
return typed_fn.expr_types.get(value.node_id, type_table.ensure_unknown())
```

This prevents a raw `Dog` `expr_types` entry from replacing the already selected
`Speaker` function return.  It also aligns the hidden signature with P1.3's
CallInfo/function-type authority.

## 4. Complete the boundary matrix

Add new tests rather than repurposing the named-function positive as lambda
coverage.

Mandatory positives, all compiling and running:

1. `Callback0<Speaker>` with a fresh `Dog(...)` block trailing value.  Assert
   `ConstructIfaceValue` in hidden MIR and runtime dispatch result.
2. Same callback with explicit `return Dog(...)`.
3. Same callback with expression body `=> Dog(...)`.
4. Direct annotated IIFE `|| -> Speaker => { Dog(...) }`.
5. A `move dog` block tail to exercise an owned local rather than only a fresh
   constructor.

Mandatory negative:

1. `Callback0<Speaker>` with a `Cat(...)` block tail where `Cat` has no impl.
   Assert exactly one clean checker diagnostic containing
   `does not implement interface`, with no traceback, MIR/SSA contract text, or
   codegen error.

The explicit-return and expression-body siblings are green before the fix; they
pin convergence of equivalent forms.  The block-tail callback and IIFE are red
before the fix and prove the repaired branch is exercised.

Keep the existing named-function positive.  It remains useful coverage of the
shared `HReturn` authority but should not claim lambda-tail coverage.

## 5. Focused gates

Run at minimum:

```text
new hidden-lambda coercion boundary tests
lang/tests/driver/test_lambda_return_inference_boundary.py
lang/tests/driver/test_implicit_callback_wrap.py
lang/tests/driver/test_callback_dynamic_dispatch.py
lang/tests/driver/test_closure_void_tail_lowering.py
lang/tests/driver/test_lambda_trailing_match_value.py
lang/tests/driver/test_try_expr_immediate_lambda.py
lang/tests/stage2/test_hidden_lambda_collected.py
```

The Void-tail and statement-form-match gates are important because the proposed
normalizer changes value-producing final `HExprStmt` nodes into function
`HReturn` nodes.  Then run the combined #1-#4 lambda-focused gate before broader
suite/corpus work.

## Boundary/version assessment

This repair does not introduce a new MIR or runtime interface representation;
`ConstructIfaceValue` is the existing supported lowering for the already
accepted concrete-to-interface return contract.  It makes hidden lambda
lowering honor the checker's existing acceptance and prevents an internal
contract diagnostic.  No ABI signature, layout, calling convention, or runtime
export changes, so no ABI bump is indicated.

The effect is user-visible because a valid program changes from compiler failure
to successful compilation.  Per the user's explicit instruction, the compiler
version has already been bumped for the user-visible work in flight; do not add
another version edit for this handoff.
