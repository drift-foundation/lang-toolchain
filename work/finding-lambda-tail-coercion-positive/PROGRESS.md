# Progress: lambda-tail coercion positive

Last updated: 2026-08-03

## Status

- [x] Classified the observed internal SSA diagnostic as `LANGUAGE_BUG`.
- [x] Scanned `doc/refactor_triggers.md`; no trigger fires.
- [x] Checked cross-team announcements; `/tmp/drift-announce` absent.
- [x] Audited the existing positive and confirmed it covers named `HReturn`, not
  a lambda trailing value.
- [x] Saved the requested `Callback0<Speaker>`/`Dog` minimal repro.
- [x] Confirmed the block-tail callback fails with `Dog` versus `Speaker` at SSA.
- [x] Confirmed hidden MIR lacks `M.ConstructIfaceValue`.
- [x] Confirmed expression-body and explicit-return callback siblings compile
  and run with exit 0.
- [x] Confirmed a direct block-tail IIFE has the same failure.
- [x] Confirmed the non-implementing negative gets one clean checker diagnostic.
- [x] Added explicitly-run red MIR and full compile/run probes under this folder.
- [x] Proposed one shared hidden-function body normalizer and authoritative
  hidden return handling.
- [ ] Move/adapt the red probes into the in-tree test suite.
- [ ] Confirm both are red immediately before the compiler change.
- [ ] Implement the shared normalizer after K's #1 changes settle.
- [ ] Add the complete positive/negative matrix.
- [ ] Run focused and combined lambda gates.

## Evidence

Red MIR boundary:

```text
hidden instructions:
  ConstString(...)
  ConstInt(... value=7)
  ConstructStruct(... struct_ty=Dog ...)

expected but absent:
  ConstructIfaceValue(... iface_ty=Speaker, value_ty=Dog)
```

Red full driver boundary:

```text
error: typecheck contract failure: SSA return type does not match declared
signature for repro::__lambda_cb_main_0_0 in entry (15 vs 16)
exit 1
```

Green isolators before the fix:

```text
repro_callback0_speaker_explicit_return.drift  compile 0, run 0
repro_callback0_speaker_expr_body.drift        compile 0, run 0
repro_iife_speaker_expr_body.drift             compile 0
```

Clean negative before the fix:

```text
repro_callback0_nonimplementing_tail.drift:15:12:
error: 'Cat' does not implement interface 'Speaker'
exit 1
```

Run the red probes by explicit path:

```bash
./.venv/bin/python3 -m pytest -q work/finding-lambda-tail-coercion-positive/red_hidden_lambda_coercion_positive.py
```

The filename intentionally does not match pytest's default `test_*.py`
discovery pattern, so a repository-root pytest run will not accidentally absorb
the expected-red handoff tests.

## Resume notes for K

1. Refresh `git diff -- lang/driftc/type_checker.py lang/driftc/driftc.py` first;
   #1 is actively changing the return authority.
2. Preserve the final shared `_type_return_value`; this finding is downstream of
   it and should route regenerated block tails through it via `HReturn`.
3. Add one helper and use it in both hidden-lambda reconstruction loops.
4. Do not copy the enclosing `TypedFn` coercion tables across the deep-copy and
   normalization boundary.
5. Keep `spec.return_type_id` / `spec.return_type` authoritative; raw expression
   types are only an `Unknown` fallback and must consult coercion marks.
6. Verify the MIR pin first.  A merely diagnostic-free `compile_stubbed_funcs`
   call is insufficient: it is already diagnostic-free today while returning
   raw `Dog` MIR.

Only files under `work/finding-lambda-tail-coercion-positive/` were created by
this research.  No compiler, runtime, stdlib, or in-tree test file was edited.
