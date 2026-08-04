# Plan: reconcile every inferred-lambda return

Refreshed: 2026-08-04, after the 0.35.0 hidden-lambda return-authority work
landed.

This is a reviewer handoff, not an implementation prescription.  The current
tree and red probes support the design below, but the implementer should verify
the invariants while editing and record any counter-evidence in `PROGRESS.md`.
In particular, prefer a smaller design if it preserves one-pass typing,
nested-lambda isolation, lowering-visible coercions, and the regression matrix.

## Current-tree confirmation

The 0.35.0 patch fixed the later hidden-function reconstruction boundary.  It
did not change the primary lambda inference path in
`lang/driftc/type_checker.py`:

- `type_expr(HLambda)` saves the current return type and installs
  `lambda_ret_type` near line 7632.
- `_find_return_expr` near line 7673 still walks syntax after typing and returns
  only the first valued `HReturn` outside nested lambdas.
- `_lambda_body_result` near line 7734 still types prefix returns while
  `return_type is None`, then chooses a trailing value or that one found return.
- `type_stmt(HReturn)` near line 12860 routes valued returns through
  `_type_return_value`, but discards its returned effective type.

The work-folder first-pass probes were rerun on HEAD after 0.35.0:

```text
./.venv/bin/python3 -m pytest -q \
  work/finding-lambda-return-reconciliation/red_first_pass_reconciliation.py

2 failed in 0.51s
```

Both failures are the intended red condition: the direct call is inferred as
`Int`, while the primary `TypeChecker.check_function` diagnostic list contains
no `String`/`Int` conflict.  A full driver compile already rejects later because
0.35.0 reconstructs and checks the hidden function against the selected `Int`
signature.  That later safety net must not be mistaken for closure of this
finding.

`doc/refactor_triggers.md` was rescanned.  No registered trigger matches a
lambda-local return-type observation collector, so this remains a focused
compiler fix.  No cross-team announcement was present at refresh time.

## Folded sibling scope: P1.3 CallInfo inference boundary

By Slawomir's approval on 2026-08-04, the closely coupled sibling
`work/finding-p13-callinfo-inference/` is part of this implementation slice.
Treat it as a second, bounded deliverable after the return collector is stable:

- rerun its no-context probes against the reconciled inference path;
- preserve the one live `HCall(fn=HLambda)` route that consumes the function
  TypeId returned by `type_expr(lam)`;
- delete the still-unreachable duplicate lambda-call branch in
  `checker/call_resolver.py` rather than transplanting its body;
- add truthful no-context CallInfo and producer-shape characterization pins;
- retain a no-context full compile/run companion; and
- correct the stale contextual-inference and stored-`HInvoke` comments in the
  existing boundary test.

Slawomir explicitly approved those comment-only edits to the existing test on
2026-08-04.  The approval does not extend to changing an existing assertion,
expected diagnostic, source program, or behavior; any such need gets a fresh
approval gate.

The sibling remains a separate finding folder for evidence ownership and later
cleanup, but it shares this focused implementation/review cycle.  Its deletion
of unreachable code is user-neutral and adds no version or ABI change beyond
the active slice.

## Required invariants

1. Every return expression is typed exactly once during the primary lambda
   visit.  Reconciliation reads captured results; it never calls `type_expr` or
   `_type_return_value` again.
2. `_type_return_value` remains the sole authority for typing a valued return
   and applying auto-try, `&T -> T`, callback wrapping, and interface coercion.
3. Existing HIR rewrites and side tables remain the lowering authorities.  The
   collector is ephemeral inference state, not a new lowering input.
4. Nested `HLambda` nodes are function boundaries.  Their returns can never
   enter an enclosing lambda's observations.
5. Value-block inference keeps its current deterministic rule: a genuine
   trailing value is the candidate.  Without one, the first valued explicit
   return is the candidate.  If none exists, the candidate is `Void`.
6. This patch validates compatibility; it does not invent a least-upper-bound
   algorithm or apply coercions after inference.  Differing fully inferred
   types are rejected.
7. `Unknown` is poison.  Comparisons involving it are suppressed so one
   upstream failure does not create return-mismatch cascades.
8. Lambdas entering body typing with an annotation or concrete contextual
   return are not run through a second compatibility pass.  Their valued
   returns already use `_type_return_value` with that expectation.  Bare-return
   shape checking remains owned by the existing phase-2 Void/return rules; do
   not duplicate it accidentally.

## 1. Land the red boundary first

Adapt the two work probes into a new in-tree file, preferably:

`lang/tests/type_checker/test_inferred_lambda_return_reconciliation.py`

Keep them as direct `TypeChecker.check_function` tests.  Confirm they fail on
the pre-fix tree because the primary diagnostic is absent.  A driver-only test
is insufficient: the 0.35.0 hidden-function re-check already makes that surface
green for the wrong authority.

Creating a new test file does not require approval.  If implementation instead
requires changing an existing test's asserted contract, stop and use the human
approval mailbox gate before editing it.

## 2. Add nested-lambda-safe observation state

An appropriate minimal record near `LambdaFnSpec` is:

```python
@dataclass(frozen=True)
class _LambdaReturnObservation:
	value: H.HExpr | None
	effective_type: TypeId
	span: Span
```

`value is None` distinguishes a bare `return;`.  If retaining the expression
object proves unnecessary, `has_value: bool` is an equally valid, smaller
representation; no later code may use it to re-type the expression.

Inside `check_function`, beside other per-function ephemeral state, add:

```python
lambda_return_observation_stack: list[list[_LambdaReturnObservation]] = []
```

For each `type_expr(HLambda)`:

1. Resolve annotations/context first.
2. Save `infer_return_from_body = lambda_ret_type is None` before body typing.
3. Allocate a local `observations` list and push that exact object immediately
   before typing the lambda body.
4. Pop it in `finally`, asserting identity if useful.  A nested lambda pushes
   its own list, so `type_stmt(HReturn)` always records to the innermost active
   lambda.

The collector should cover both `body_expr` and `body_block` visits even though
only a block can directly contain `HReturn`; doing so keeps stack lifetime tied
to the lambda boundary rather than to one body representation.

Do not append a sentinel collector for the surrounding named function.  Named
functions and the later standalone hidden-function check must see an empty
stack and retain their existing behavior.

## 3. Record `HReturn` during its only typing pass

In `type_stmt(HReturn)`, preserve the effective type returned by the existing
authority:

```python
effective_ty = self._void
if stmt.value is not None:
	...
	effective_ty = _type_return_value(...)
if lambda_return_observation_stack:
	lambda_return_observation_stack[-1].append(
		_LambdaReturnObservation(
			value=stmt.value,
			effective_type=effective_ty,
			span=getattr(stmt, "loc", Span()),
		)
	)
```

Details that matter:

- Read `stmt.value` after `_type_return_value`; auto-try, dereference, or
  callback wrapping may replace the HIR slot.
- Store the returned effective type.  Do not rediscover it later from
  `expr_types`; the direct result remains valid after an arm-local scope pops
  and already reflects any expected-return coercion.
- Store no duplicate coercion metadata.  The rewritten HIR plus existing
  `iface_coercions` and CallInfo tables remain authoritative downstream.
- Record bare `return;` as `Void`.  In an inferred lambda it must disagree with
  a non-Void candidate, while all-bare returns still infer `Void`.

## 4. Remove the syntax-walk selector

Delete `_find_return_expr`.  Pass the local observation list into
`_lambda_body_result`, or close over that exact list without consulting the
stack after nested typing.

Candidate selection after the block has been typed remains:

1. A genuine non-terminal value tail: use the one-pass `_type_return_value`
   result already computed for that tail.
2. No value tail (including a statement-form match or terminal-`throws` tail):
   use the first observation whose `value is not None`.
3. No valued observation: use `self._void`.

The final statement must still be typed according to its existing category:

- ordinary trailing `HExprStmt`: once in value context;
- parser-authoritative statement-form `HMatchExpr`: once through `type_stmt`;
- explicit return/binding/control statement: once through `type_stmt`;
- terminal-`throws` call tail: type once to establish CallInfo, then treat it as
  no value and select from observations or `Void`.

This preserves 0.35.0's shared `hir_flow` divergence handling and removes the
post-scope AST traversal entirely.

## 5. Reconcile genuinely inferred returns

After body typing and candidate selection:

1. If `infer_return_from_body`, install the candidate as `lambda_ret_type`.
2. Compare every observation's `effective_type` with the candidate.
3. Skip a comparison if either side is `Unknown`.
4. Accept TypeId equality or normalized type-key equality so aliases and
   cross-package `FORWARD_NOMINAL` placeholders behave like the existing return
   contract.
5. Diagnose each incompatible observation at its own stored return span.

Extract the normalized equality currently nested inside `_type_return_value`
into one local helper shared by `_type_return_value` and reconciliation.  There
is a second copy in the typed-`HLet` initializer path near line 12309; folding
that copy into the helper is reasonable if mechanically safe, but it is not a
condition of this finding and should not broaden the patch unnecessarily.

Recommended stable diagnostic:

```text
E-LAMBDA-INFERRED-RETURN-MISMATCH
return type 'String' does not match inferred lambda return type 'Int'
```

For a bare return against a non-Void candidate, either render its observed type
as `Void` using the same message or use an equally stable, explicit bare-return
message.  Tests should pin the chosen wording and span.  Keep the selected
candidate on the lambda/call boundary after diagnosing; poisoning it to
`Unknown` risks unrelated cascades and is unnecessary because compilation
already fails.

Do not call `_type_return_value` in this reconciliation pass.  With no declared
or contextual target there is no principled destination for interface,
callback, reference, or numeric coercion, and applying one late would require
new lowering-visible rewrites.  If implementation evidence shows an existing
language rule that requires such a join, stop and record that counterexample
rather than silently inventing one here.

## 6. Regression matrix

### Primary-boundary negatives

1. Prefix `return "x"` plus trailing `1`.
2. Statement-only branches returning `1` and `"x"`.
3. Statement-form match with incompatible returns; include an arm-local binder
   return so the type must have been captured before the arm scope popped.
4. Bare `return;` on one path plus a valued tail or valued return on another.
5. An upstream-Unknown return paired with a concrete candidate: preserve the
   upstream diagnostic and add no inferred-return cascade.

For the first two, assert the call remains inferred as `Int` and the primary
checker emits exactly one stable mismatch diagnostic.  This proves authority
placement, not merely eventual rejection.

### Driver negative

Compile the minimal source repro and assert:

- nonzero exit;
- exactly one inferred-return mismatch;
- no traceback, `MIR contract failure`, LLVM error, or duplicate
  declared-return diagnostic from the later hidden pass.

### Positive and isolation coverage

1. Prefix `return Int` plus trailing `Int`; full compile/run invokes both paths.
2. Statement-only `Int` returns in both branches.
3. Statement-form match whose arm-local returns all agree.
4. Nested lambda returning `String` inside an outer lambda returning `Int`; the
   inner observation must not enter the outer collector.  Include a direct
   primary-checker pin and a compile/run companion.
5. All bare returns infer `Void` and invoke successfully.
6. Existing annotated/contextual interface, callback, auto-try, and `&T -> T`
   return-coercion positives stay green; the collector must not replace or
   duplicate their lowering-visible marks.
7. Existing 0.35.0 terminal-`throws`, statement-form match, and divergent-body
   finalization pins stay green.

At minimum rerun:

```text
lang/tests/type_checker/test_inferred_lambda_return_reconciliation.py
lang/tests/driver/test_lambda_return_inference_boundary.py
lang/tests/driver/test_lambda_return_inference.py
lang/tests/driver/test_hidden_lambda_return_boundary.py
lang/tests/driver/test_drift_query_slice12_ices.py
lang/tests/driver/test_lambda_trailing_match_value.py
lang/tests/driver/test_try_expr_immediate_lambda.py
lang/tests/driver/test_stored_capturing_lambda_diagnostic.py
```

Run focused gates first.  Broader/full-suite work follows review, not the
initial implementation handoff.

## Boundary, version, and documentation assessment

This fix changes user-visible acceptance: an inconsistent inferred lambda that
could pass the primary checker (and historically could be miscompiled through
some routes) is rejected at the authoritative visit.  It is being added to the
same unreleased/uncertified 0.35.0 compiler train that already carries the
lambda-return work.  The 0.35.0 minor boundary therefore already accounts for
this user-visible slice; SemVer does not require a new minor for every commit
made before that release is certified.

- `DRIFTC_VERSION`: remain at the pending, unreleased `0.35.0`;
- `DRIFT_RT_ABI_VERSION`: unchanged at 22, because no runtime signature,
  layout, calling convention, or ownership/drop boundary changes;
- a `doc/history.md` entry describing the primary-authority correction and
  diagnostic behavior, folded into the pending 0.35.0 release history as
  appropriate;
- a release announcement under `/tmp/drift-announce/` after implementation is
  reviewed and ready.

No language-spec edit is expected: spec section 22.1.1 already says an
unannotated lambda return is inferred from its body, which entails reconciling
all returning paths.  If implementation discovers that candidate selection or
coercion requires a different language contract, stop for Slawomir's approval;
do not alter the spec silently.
