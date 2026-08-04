# Plan: preserve hidden-lambda return semantics through reconstruction

Refreshed against mainline 0.34.2 on 2026-08-04. This supersedes the
2026-08-03 handoff plan; line numbers below match the current tree but remain
navigational. The finding is a `LANGUAGE_BUG` with checker→MIR boundary impact
and one silent wrong-result shape.

This finding does **not** own the separate queued inferred-lambda
prefix-return reconciliation bug. Do not expand this patch into a second
first-pass lambda-body inference authority.

## 0. Revalidate and pin red behavior first

Read the entire finding folder and `EVIDENCE-REFRESH-2026-08-04.md`. Re-run the
work-only probes before source changes. Scan `doc/refactor_triggers.md`; the
current scan found no matching trigger, but implementation must re-check if a
different root cause appears.

Move/adapt the red cases into a new in-tree driver/boundary test module before
fixing production code. New test files are preferred so existing test edits do
not become an accidental approval bypass.

Required pre-fix reds:

1. `Callback0<Speaker>` with a `Dog` block trailing value: hidden MIR lacks
   `M.ConstructIfaceValue` and full compilation fails with an internal SSA
   `Dog`/`Speaker` signature diagnostic.
2. Direct annotated `|| -> Speaker => { Dog(...) }` IIFE: same internal SSA
   mismatch.
3. Stored `|k| -> Int` with a throwing value-match tail: build succeeds but the
   process exits **0**, while the source contract requires **5**. Pin exact exit
   5 after the fix; diagnostic-free compilation alone is insufficient.
4. Stored `|n| -> Int` ending in a terminal-`throws` call: compilation reaches
   LLVM with `FnResult<Unknown>` and raises a Python traceback. Pin clean
   compile/run exit 0 after the fix and explicitly forbid traceback/internal
   contract text.

Also retain the clean pre-fix `Cat` negative and green explicit-return /
expression-body controls. Record exact red/green outcomes in `PROGRESS.md`.

## 1. Add one shared hidden-function body normalizer

The current reconstruction sites are in `lang/driftc/driftc.py` near lines
6758–6764 (`HiddenLambdaSpec`) and 7775–7782 (`LambdaFnSpec`). Both currently
convert only `body_expr` to `HReturn`; a block trailing value remains
`HExprStmt`, so standalone `check_function(return_type=...)` types it as a
discarded statement and never records return coercions.

Add one private helper used by **both** worklists. It operates on the already
deep-copied lambda and returns the standalone function body before
`normalize_hir`:

- expression body → one `HReturn(value=...)`;
- block ending in a genuine value-producing `HExprStmt` → replace only that
  last statement with `HReturn(value=...)`;
- existing `HReturn`, empty/value-less bodies, and non-expression statements →
  preserve;
- statement-form `HMatchExpr` → preserve as a statement because its internal
  explicit returns are the exits;
- a terminal-`throws` tail call → preserve as an `HExprStmt`; it never yields a
  return value and wrapping it in `HReturn` would invent a Void/Int mismatch.

Do not detect terminal calls by function-name spelling or by a new ad-hoc
walker. Use the recorded `CallInfo.sig.declared_terminal_throws`/signature
authority already consumed by `hir_flow` and lowering. The two spec routes have
different containers (`LambdaFnSpec.call_info_by_callsite_id` versus the
origin/hidden typed function state), so design the helper to accept a small
terminal-call predicate rather than teaching it worklist internals. Cover
`HCall`, `HMethodCall`, and `HInvoke` if their recorded `CallInfo` can carry the
terminal flag.

Preserve the original tail span on the synthesized `HReturn`. Run
`normalize_hir` **after** conversion so node/callsite normalization sees the
final function shape. Do not mutate or retype the original enclosing lambda;
the re-check is for the independently synthesized hidden function.

## 2. Keep each spec's return type authoritative

`HiddenLambdaSpec` already starts from `spec.return_type_id` and falls back to
`_hidden_lambda_ret_type` only when genuinely `Unknown`; preserve that policy.

The captureless `LambdaFnSpec` worklist currently overwrites its checked/spec
return unconditionally at `driftc.py` near line 7847:

```python
lambda_ret_type = _hidden_lambda_ret_type(lambda_body, lambda_typed_fn, shared_type_table)
```

Bring it to parity:

```python
lambda_ret_type = spec.return_type
if shared_type_table.get(lambda_ret_type).kind is TypeKind.UNKNOWN:
	lambda_ret_type = _hidden_lambda_ret_type(lambda_body, lambda_typed_fn, shared_type_table)
```

Handle lookup failure explicitly and narrowly; do not use a broad exception to
silently replace a concrete spec return.

If `_hidden_lambda_ret_type` remains as the Unknown-only fallback, make its
effective expression lookup prefer a local lowering-visible interface
coercion:

```python
coerced = typed_fn.iface_coercions.get(value.node_id)
if coerced is not None:
	return coerced
return typed_fn.expr_types.get(value.node_id, type_table.ensure_unknown())
```

Apply that rule to both `HReturn.value` and value-producing `HExprStmt` fallback
shapes. Do not copy the enclosing function's side tables into the hidden
`TypedFn`: the body is deep-copied, capture-remapped, normalized, and checked
as its own function. Routing a genuine tail through local `HReturn` checking
must create the mark on the correct hidden HIR instance.

## 3. Prove the checker/lowering boundary, not only diagnostics

The new boundary tests must cover:

### Interface-return matrix

1. Callback block tail with fresh `Dog(...)`: inspect hidden MIR for
   `ConstructIfaceValue(iface_ty=Speaker, value_ty=Dog)` and compile/run exit 0.
2. Callback explicit `return Dog(...)`: compile/run control.
3. Callback expression body `=> Dog(...)`: compile/run control.
4. Direct annotated block-tail IIFE: compile/run exit 0.
5. Callback block tail `move dog`: compile/run owned-local control.
6. Non-implementing `Cat` block tail: exactly one clean checker diagnostic;
   no traceback, MIR/SSA contract text, or codegen error.

### Spec-return/flow matrix

7. Stored throwing value-match tail: exact runtime exit 5, proving the match
   value is returned rather than discarded and the declared `Int` was not
   overwritten with `Void`.
8. Stored terminal-throws tail call: clean compile/run exit 0, proving the call
   stays statement-form while the declared `Int` spec remains authoritative.
9. A structural helper pin showing: expression body converts, ordinary block
   value tail converts, statement-form match does not, terminal call does not,
   and empty/value-less body does not.

Keep the existing named-function interface positive: it covers the shared
`HReturn` authority but must not be relabeled as hidden-lambda coverage.

The existing comment in
`lang/tests/driver/test_stored_capturing_lambda_diagnostic.py` says the stored
terminal form is blocked by this finding. That statement becomes stale after
the fix. Editing that existing test/comment requires Slawomir's explicit test
edit approval under repo policy; request it through `APPROVAL-PENDING-*` before
touching the file. New tests can land independently while approval is pending.

## 4. Focused verification

Run at minimum:

```text
new hidden-lambda return-boundary test module
lang/tests/driver/test_lambda_return_inference_boundary.py
lang/tests/driver/test_stored_capturing_lambda_diagnostic.py
lang/tests/driver/test_implicit_callback_wrap.py
lang/tests/driver/test_callback_dynamic_dispatch.py
lang/tests/driver/test_closure_void_tail_lowering.py
lang/tests/driver/test_lambda_trailing_match_value.py
lang/tests/driver/test_try_expr_immediate_lambda.py
lang/tests/driver/test_stmt_position_iife.py
lang/tests/driver/test_throws_terminal_body_flow_phase2.py
lang/tests/stage2/test_hidden_lambda_collected.py
```

The Void-tail, statement-form-match, direct-IIFE, and terminal-flow suites are
mandatory because the normalizer changes the last-statement shape. Run
`git diff --check` and inspect that no call-resolver body-inference path or
interface-specific MIR special case was introduced.

Do not run long full-corpus verify/promotion during this iteration; Slawomir is
batching those after the queued findings. A quick `ownership-corpus-check` is
allowed if it does not stall the next handoff, but never edit/promote the
baseline manually.

## 5. Version, history, ABI, and announcement

This is a new user-visible fix after 0.34.2 was mainlined: valid programs move
from compiler failure/traceback or silent wrong result to correct execution.
Per repository policy, bump `DRIFTC_VERSION` from **0.34.2 to 0.35.0** before
staging. If another already-approved queued finding has established 0.35.0 on
the same unmainlined branch, do not bump again; record that this slice shares
the pending minor.

No runtime-exported signature, data layout, calling convention, or ABI shape
changes: keep `DRIFT_RT_ABI_VERSION` at **22**.

Add a self-contained `doc/history.md` 0.35.0 entry describing the three hidden
return failures, shared normalizer/spec-return authority, and boundary tests.
Publish the cross-team release announcement required by `AGENTS.md` under
`/tmp/drift-announce/<iso-utc>-drift-lang-release-notes.md` after the contract
is implemented and reviewed; consume any announcements present before work.

## Completion criteria

- All four pre-fix red behaviors are pinned and green post-fix.
- Hidden `Dog → Speaker` returns contain the lowering-visible interface
  construction and dispatch correctly at runtime.
- Stored throwing value-match returns 5, not 0.
- Stored terminal-throws tail produces no Unknown FnResult or traceback.
- Equivalent lambda body forms converge; statement/terminal/void forms remain
  correctly unwrapped.
- One shared reconstruction helper serves both worklists; concrete spec return
  types cannot be overwritten by raw tail types.
- Focused gates, version/history/announcement duties, and diff review are
  complete.
