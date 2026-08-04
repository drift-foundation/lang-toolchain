# Plan: partition extracted-lambda callsite side tables

Status: reviewer proposal, awaiting implementer revalidation.

## 1. Reproduce red first

Turn `repro_nested_callback.drift` into a new regression file before changing
the compiler.  Prefer a new driver test module so no approval to alter an
existing test is needed.

The primary red assertion should compile the source and require success; on
the pre-fix tree it must instead fail with
`E_INTRINSIC_CALLINFO_MISSING_NODE`.  The final positive must link and run the
binary, asserting exit 0, because the change affects lowering-visible CallInfo
ownership.

Also add a structural boundary pin if practical: after checking/finalizing a
stored lambda, every CallInfo entry attached to the parent `TypedFn` should
have an owned call node reachable from that parent's finalized HIR.  This pin
should distinguish the parent map from the LambdaFnSpec/hidden-function map,
not merely suppress the current intrinsic diagnostic.

## 2. Revalidate the ownership hypothesis

Confirm on the implementation tree:

- callsites 0-2 are written while the deferred outer lambda is typed;
- `_apply_fnptr_consts` removes their source subtree from the parent body;
- the parent `TypedFn` nevertheless detaches those entries;
- the extracted lambda is later independently checked and obtains the body
  CallInfo it needs; and
- `LambdaFnSpec.call_info_by_callsite_id` is used before that re-check by
  `_hidden_lambda_terminal_call_predicate`.

If one of these facts is false after closer instrumentation, record the
counter-evidence in child `PROGRESS.md` and revise the patch shape rather than
following this plan mechanically.

## 3. Recommended patch boundary

The smallest plausible authority is the finalized `TypedFn` construction in
`TypeChecker.check_function`, after `_apply_fnptr_consts(body)` has replaced
extracted lambdas.

Proposed approach:

1. Collect callsite IDs reachable from the finalized parent body using the
   ownership traversal appropriate to MIR lowering.  A default full walk of
   the post-rewrite body is a candidate: extracted lambda bodies are gone,
   while immediate-lambda bodies that remain in the HIR stay represented.
2. Detach `TypedFn.call_info_by_callsite_id` with only those owned/reachable
   entries.
3. Apply the same ownership rule to other callsite-indexed tables that travel
   with `TypedFn`, especially `instantiations_by_callsite_id`; do not leave a
   second stale map merely because this repro trips only intrinsic CallInfo.
4. Preserve a separate snapshot or filtered lambda-local view for
   `LambdaFnSpec` if its pre-recheck terminal-call classification needs it.
   Do not prune the shared mapping in place if the spec still aliases it.

An alternative implementation may partition entries at collection time, or
give nested-lambda typing an explicit checker-state boundary.  That is likely
cleaner architecturally but broader.  Prefer it only if the post-rewrite
partition cannot state the true ownership contract without special cases.

Do not fix this by allowing `_validate_intrinsic_callinfo` to ignore missing
nodes.  That guard is the evidence that an intrinsic lowering side table and
its HIR disagree.

## 4. Boundary matrix

Required:

1. Stored captureless outer lambda containing `core.callback0`: full
   compile/run exit 0 (the supplied repro).
2. Parent function with both a direct `core.callback0` and a stored outer
   lambda containing another callback: the parent entry remains, the extracted
   entry moves/is regenerated for the synthetic function, and both execute.
3. Immediate IIFE containing `core.callback0`: stays green, proving the filter
   did not equate every nested lambda with an extracted function boundary.
4. Structural map pin: no surplus parent CallInfo or callsite instantiation
   entries after extraction; every retained entry resolves to an owned source
   call.
5. Existing terminal-`throws` lambda-tail tests remain green, protecting the
   `LambdaFnSpec` predicate input.

If a negative contract test is useful, exercise the validator directly with a
genuinely orphaned intrinsic CallInfo and confirm it still reports
`E_INTRINSIC_CALLINFO_MISSING_NODE`.  This proves the guard was preserved, not
relaxed.

## 5. Focused verification

At minimum:

```text
lang/tests/driver/<new nested callback ownership test>
lang/tests/type_checker/test_lambda_callinfo_inference_boundary.py
lang/tests/driver/test_hidden_lambda_return_boundary.py
lang/tests/driver/test_lambda_terminal_throws_phase.py
lang/tests/driver/test_stored_capturing_lambda_diagnostic.py
lang/tests/stage1/test_node_ids_and_callinfo.py
```

Then rerun the five compiler smoke suites used by the parent implementation.
The full suite remains a post-review gate.

## 6. Version/spec/ABI

- No spec change is expected: this repairs compiler bookkeeping for an already
  valid lambda/callback composition.  Stop for Slawomir if investigation
  instead implies a language-contract change.
- Keep the pending, unreleased `DRIFTC_VERSION` at 0.35.0; do not bump again
  within the same uncertified release train.
- ABI remains 22 unless the implementation unexpectedly changes a runtime
  signature, layout, calling convention, or ownership/drop boundary.
- Add a concise line to the pending 0.35.0 history entry if the fix lands.

