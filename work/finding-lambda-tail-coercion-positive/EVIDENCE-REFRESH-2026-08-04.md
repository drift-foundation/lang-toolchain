# Current-tree evidence refresh — hidden lambda return boundary

Captured: 2026-08-04T15:57:18Z

Tree: mainline 0.34.2 after the divergent-lambda/shared-flow work and cleanup.

## Priority triage

The saved probes were rerun for the queued findings:

- lambda-tail coercion: still red at MIR and full-driver boundaries;
- lambda-return reconciliation: still red in the primary type-check boundary
  (two missing diagnostics), though later hidden checking rejects the surface
  repro;
- causal Unknown suppression: still red (two independent diagnostics lost);
- P1.3 CallInfo characterization: 4/4 green;
- true statement-position throwing IIFE: previously green and remains a lower
  priority test gap pending its own revalidation.

The hidden-tail finding is selected next because it combines a valid-program
compiler failure, an internal traceback, and a silent wrong result.

## Required interface coercion remains lost

Command:

```bash
./.venv/bin/python3 -m pytest -q \
  work/finding-lambda-tail-coercion-positive/red_hidden_lambda_coercion_positive.py
```

Result: 2 failed.

The hidden callback MIR contains only:

```text
ConstString
ConstInt(value=7)
ConstructStruct(struct_ty=Dog)
```

It has no `ConstructIfaceValue`. The full driver fails with:

```text
typecheck contract failure: SSA return type does not match declared signature
for repro::__lambda_cb_main_0_0 in entry (Dog vs Speaker)
```

The direct block-tail IIFE fails by the same route for
`repro_iife::__lambda_main_0_0`.

## Sibling-form matrix on current main

```text
repro_callback0_speaker_tail.drift             build 1 (Dog/Speaker SSA contract)
repro_callback0_speaker_explicit_return.drift  build 0, run 0
repro_callback0_speaker_expr_body.drift        build 0, run 0
repro_iife_speaker_tail.drift                   build 1 (Dog/Speaker SSA contract)
repro_iife_speaker_expr_body.drift              build 0, run 0
repro_callback0_nonimplementing_tail.drift      build 1, one clean Cat diagnostic
```

The negative remains:

```text
'Cat' does not implement interface 'Speaker'
```

No traceback is involved in that negative.

## Two current stored-lambda failures

`repro_stored_throwing_value_match_void_ret.drift` builds and the process exits
0. The program returns the lambda result directly; correct semantics require
exit **5** (`f(4)` selects the false match arm `(4 + 1)`). This confirms the
hidden signature/body path still treats the value match as Void/discarded and
silently returns the wrong value.

`repro_stored_terminal_call_unknown_ret.drift` fails compilation with a raw
Python traceback ending in:

```text
NotImplementedError: LLVM codegen v1: FnResult ok type UNKNOWN is not supported yet
```

The equivalent named-function and direct-IIFE terminal-call forms are already
green; only the stored `LambdaFnSpec` reconstruction overwrites the declared
`Int` with the raw/Unknown tail result.

## Current code path

- `driftc.py:6383` `_hidden_lambda_ret_type` reads raw `expr_types` and ignores
  `TypedFn.iface_coercions`.
- `driftc.py:6758–6764` (`HiddenLambdaSpec`) converts `body_expr` to `HReturn`
  but leaves `body_block` unchanged before standalone checking.
- `driftc.py:7300–7306` correctly treats concrete
  `spec.return_type_id` as authoritative and falls back only for Unknown.
- `driftc.py:7775–7782` repeats the asymmetric body reconstruction for
  `LambdaFnSpec`.
- `driftc.py:7847` unconditionally overwrites `spec.return_type` via
  `_hidden_lambda_ret_type`.
- `type_checker.py:7734+` and `:12076+` already provide the primary lambda-tail
  and shared return-coercion authorities; the fix must route regenerated value
  tails through them as local `HReturn`, not create another coercion policy.
- `hir_to_mir.py:5944+` lowers lambda block tails; it already distinguishes
  terminal calls from value tails and works for explicit `HReturn` siblings.

The original normalizer proposal needs one current-tree correction: a final
terminal-`throws` call must **not** be wrapped in `HReturn`. Terminal status is
semantic CallInfo/signature data and should enter a shared helper through a
predicate; the helper must not duplicate call resolution.

## Trigger and announcement refresh

`doc/refactor_triggers.md` was searched for hidden-lambda, closure, coercion,
return, side-table, and checker/lowering triggers. No entry's trigger condition
matches this reconstruction/spec-authority defect. The generic HIR visitor and
ownership-authoring entries do not fire.

No `/tmp/drift-announce` release-note file was present at refresh time.
