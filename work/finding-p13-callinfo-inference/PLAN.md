# Plan: make P1.3 reachable, honest, and boundary-pinned

Refreshed: 2026-08-04 against 0.35.0.  This sibling is folded into the active
`finding-lambda-return-reconciliation` implementation slice.  Complete its
bounded cleanup after the return-observation collector is stable, then hand
both findings back for one combined review.

The current tree still has the live branch near line 5100 and the unreachable
duplicate near line 6019.  The work probes were rerun after 0.35.0 and remain
green: `4 passed in 0.52s`.

## 1. Add true no-context CallInfo tests first

Adapt `probe_callinfo_inference.py` into a new in-tree file such as:

`lang/tests/type_checker/test_lambda_callinfo_inference_boundary.py`

Use an empty `CallableRegistry` in the synthetic-HIR harness; `check_function`
only fills missing callsite IDs in that mode.  Each inference call must appear in
an expression statement or an unannotated `HLet`, so `expected_type` is `None`.

Pin these boundaries independently:

### A. Live direct IIFE route

Construct `HCall(fn=HLambda(...value-block tail Int...), args=[])` and assert:

- no diagnostics;
- the call has a callsite ID and CallInfo;
- `info.sig.user_ret_type == Int`;
- `info.target.kind is INDIRECT`;
- `expr_types[call.node_id] == Int`; and
- the lambda expression's function type is not `Unknown`.

### B. Actual stored-source route

Construct an unannotated `HLet(name="f", value=HLambda(...Int tail...))`, then an
`HCall(fn=HVar("f"), args=[])` in a no-context position.  Assert the same return
boundary and that the indirect target's callee ID is the resolved binding ID.

This is the route the existing `val f = ...; f()` source test actually uses.

### C. Explicit HInvoke contract

Construct synthetic `HInvoke(callee=HLambda(...Int tail...), args=[])` with no
expected result and assert its CallInfo return is `Int`.  Label it accurately as
a synthetic-HIR contract test; do not claim ordinary stored-lambda syntax emits
this node.

### D. Producer-shape pin

Parse a small source containing direct and stored invocations.  Assert the
direct node is `HCall(fn=HLambda)`, the stored node is `HCall(fn=HVar)`, and
neither is `HInvoke`.  This prevents later tests/comments from confusing HIR
node class with the CallInfo target kind.

These are characterization tests and are green on the current live code.  There
is no legitimate red behavioral regression because the proposed source change
is deletion of unreachable code.  If any becomes red once #1/#2 settles, stop
and treat that as a newly demonstrated `LANGUAGE_BUG` before proceeding.

## 2. Delete the unreachable duplicate branch

In `lang/driftc/checker/call_resolver.py`, delete the entire second block near
6019:

```python
if isinstance(expr.fn, H.HLambda):
	...
	return record_expr(expr, call_ret)
```

This includes K's added `if lambda_ret_type is None: type_expr(lam) ...` logic.
Do not move it to the 5100 branch.  That live branch already consumes the
function TypeId returned by `type_expr(expr.fn, expected_type=callee_expected)`,
extracts `fn_sig_ret`, and records it.

After deletion there should be exactly one `HCall(fn=HLambda)` authority in
`resolve_call_expr`.

Do not modify the separate `HInvoke` implementation in `type_checker.py`; the
no-context probe demonstrates it already consumes the inferred function return
correctly.

## 3. Correct the existing test claims

The first two tests in
`lang/tests/driver/test_lambda_return_inference_boundary.py` remain useful as
contextual-return and compile/run pins, but their comments currently claim they
prove inference and that the stored form is an `HInvoke` route.

Reframe them accurately:

- direct annotated binding: contextual result propagation for an IIFE;
- stored annotated binding: contextual result propagation through a pending
  stored lambda / indirect `HCall(fn=HVar)`.

The new no-context CallInfo tests own the P1.3 inference claim.  Do not use a
downstream arithmetic expression as the only boundary assertion.

Because the repository forbids stale boundary comments, update the misleading
module prose and comments in the same patch.  Keep the test behavior; this is a
truthfulness correction, not weakening coverage.

Slawomir explicitly approved these comment-only edits on 2026-08-04.  This
approval covers only the stale prose that mislabels contextual typing as
inference and an `HCall(fn=HVar)` source route as `HInvoke`.  It does not approve
changes to existing test source, assertions, expected diagnostics, or behavior;
use a fresh approval handoff if any of those become necessary.

## 4. Retain a full compile/run companion

Add or adapt the included no-context source as a driver test:

```drift
val direct = (|| => { 6 })();
val f = || => { 7 };
val stored = f();
return direct + stored - 13;
```

Both call results must be bound without annotations.  Compile, link, run, and
assert exit 0.  This complements the CallInfo assertions with the required
lowering-visible acceptance check.

## 5. Focused gates

Run the new boundary tests plus at least:

```text
lang/tests/driver/test_lambda_return_inference_boundary.py
lang/tests/driver/test_lambda_return_inference.py
lang/tests/stage1/test_node_ids_and_callinfo.py
lang/tests/driver/test_try_expr_immediate_lambda.py
lang/tests/driver/test_lambda_trailing_match_value.py
```

Then run the combined reconciliation/P1.3 lambda-focused gate.  The important
audit is:

- one live `HCall(fn=HLambda)` resolver;
- no second body-inference path;
- no-context CallInfo returns are concrete;
- stored source is tested through its actual HIR node and indirect target; and
- positive programs compile and execute.

## Boundary/version assessment

Deleting unreachable code and strengthening tests is proven user-neutral on the
current tree.  It changes no accepted source, diagnostic, serialized format,
runtime signature, layout, calling convention, or ABI boundary.  No compiler or
ABI version bump is indicated.  This work is folded into the same pending,
unreleased 0.35.0 train; do not add a version change for this handoff.
