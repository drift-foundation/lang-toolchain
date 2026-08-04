# Finding: extracted stored lambda leaves intrinsic CallInfo in its parent

Parent: `work/finding-lambda-return-reconciliation`

Discovered: 2026-08-04 while validating nested-lambda isolation for the
inferred-return reconciliation slice.

Classification: `LANGUAGE_BUG`

Suspected subsystem: TypeChecker function-boundary ownership of
callsite-indexed side tables, specifically the interaction between deferred
stored-lambda typing, `_apply_fnptr_consts`, `LambdaFnSpec`, and
`driftc.py::_validate_intrinsic_callinfo`.

This is reviewer research, not an implementation specification.  The observed
failure and side-table/body mismatch below are reproduced facts.  The proposed
partitioning fix is a falsifiable hypothesis; the implementer should replace it
if a different ownership point better fits the pipeline.

## Minimal source and observed result

`repro_nested_callback.drift` stores a captureless outer lambda.  The outer
lambda constructs a captureless `core.callback0`, calls it, and returns an
`Int`.  The outer lambda is then invoked.

On the current worktree:

```text
./.venv/bin/python3 -m lang.driftc.driftc \
  work/finding-lambda-return-reconciliation/findings/finding-nested-lambda-intrinsic-callinfo/repro_nested_callback.drift \
  --entry repro::main --target-word-bits 64 --stdlib-root stdlib \
  -o /tmp/drift-nested-callback-repro

exit 1
<unknown location>:?:?: error: intrinsic CallInfo is missing source call node [E_INTRINSIC_CALLINFO_MISSING_NODE]
```

This is a valid source shape reaching a user-visible internal contract
diagnostic.  It therefore violates the repository's Checker / Lowering
Contract Rule 2, independent of whether the eventual root fix is in the
checker or driver orchestration.

K reports that the same source produced the same failure from a pre-fix
`git archive HEAD` scratch tree, so it is not believed to have been introduced
by the return collector.  The reviewer independently reproduced it on the
current worktree and traced the current-tree state below.  Pre-existence does
not make it optional under the repository/user bug policy.

## Confirmed current-tree state

A read-only wrapper around `_validate_intrinsic_callinfo` printed the `repro`
function's finalized body calls and CallInfo map immediately before the failing
validation:

```text
TRACE_FN repro::main
TRACE_INFO [(0, INTRINSIC, callback0),
            (1, INDIRECT, None),
            (2, DIRECT, None),
            (3, INDIRECT, None)]
TRACE_NODES [(22, HCall, 3, outer)]
```

The callback constructor at callsite 0, `inner.call()` at 1, and
`s.byte_length()` at 2 belonged to the stored outer lambda's body when that
body was first typed.  In the finalized parent `TypedFn.body`, only the outer
invocation at callsite 3 remains reachable.  Nevertheless, the parent
`TypedFn.call_info_by_callsite_id` still contains all four entries.

The relevant path is:

1. An unannotated stored lambda is deferred in
   `type_checker.py`'s `pending_lambda_by_binding` (`HLet`, currently near
   line 12318).
2. Its first invocation types the lambda body in the enclosing function's
   checker state (near lines 9985-10001).  Calls inside that body therefore
   populate the enclosing `call_info_by_callsite_id` map.
3. Typing the lambda installs a `LambdaFnSpec`; the spec currently receives
   the same enclosing call-info mapping (near lines 7987-8025).
4. `_apply_fnptr_consts` (near lines 13672-13825) replaces the stored
   `HLambda` node with an `HFnPtrConst`.  Its body, including callsites 0-2,
   is no longer reachable from the finalized parent HIR.
5. `TypedFn` detaches the whole unpartitioned CallInfo map (near line 13848).
6. The TypeChecker's local completeness scan deliberately does not descend
   into lambda bodies and only checks `source call -> CallInfo`; it does not
   detect surplus `CallInfo -> no source call` entries.
7. `driftc.py::_validate_intrinsic_callinfo` performs the reverse check for
   intrinsic entries.  It walks the finalized parent body, cannot find
   callsite 0, and emits `E_INTRINSIC_CALLINFO_MISSING_NODE` before the lambda
   worklist can re-check and lower the extracted body as its own function.

The reverse intrinsic validation is doing useful work: deleting or weakening
it would conceal a broken side-table ownership invariant rather than fix it.

## Scope and risk

Although `callback0` is the minimal visible trigger, the underlying mismatch
is broader: every call inside an extracted stored lambda is initially retained
in the parent's CallInfo map after its HIR subtree is replaced.  Non-intrinsic
surplus entries happen not to trip `_validate_intrinsic_callinfo`, but they are
still owned by the wrong finalized function and can contaminate future
callsite-indexed consumers.

Immediate-invocation lambdas are an important counter-boundary.  Their bodies
may remain reachable in the parent HIR and be lowered through the parent's
tables.  A fix must partition by the finalized ownership/reachability model,
not blindly delete every entry lexically found below any `HLambda`.

## Refactor-trigger result

`doc/refactor_triggers.md` was rescanned on 2026-08-04 for CallInfo, intrinsic,
lambda, hidden-function, HIR-walker, and side-table triggers.  No registered
trigger matches this failure.  The likely deliverable remains a focused
function-boundary side-table ownership fix rather than a listed larger
refactor.

## Contract to preserve

- Keep `_validate_intrinsic_callinfo` strict: a finalized function must not
  carry an intrinsic CallInfo entry without its owned source call node.
- The extracted lambda's independent re-check must still recreate/retain all
  CallInfo needed to lower its body.
- Terminal-`throws` classification performed before the independent re-check
  must retain whatever lambda-local CallInfo snapshot it genuinely consumes.
- Direct parent calls and calls inside immediate (non-extracted) lambdas must
  not be pruned.
- Do not copy a single unpartitioned mutable map between parent and synthetic
  function as a substitute for explicit ownership.
- No user diagnostic should expose an internal CallInfo contract failure for
  this valid program.

