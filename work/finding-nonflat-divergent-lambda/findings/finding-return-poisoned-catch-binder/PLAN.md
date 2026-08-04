# Proposed plan: restore lexical authority for unresolved HVars

This plan is reviewer guidance and is deliberately falsifiable.

1. Reproduce the existing e2e failure and inspect the HVar's `binding_id`
   before and after phase-1 type checking.
2. Confirm that the function-wide `binding_names` fallback, rather than a
   different mutation, binds the post-catch `e` to the popped catch binder.
3. Inventory callers/producers that may rely on the fallback. Pay particular
   attention to synthetic HIR, normalization/reconstruction passes, params,
   match/catch binders, and stub-pipeline tests.
4. Add a focused regression before the fix if the existing e2e does not expose
   the phase-1 mutation precisely enough. Include a non-catch block-scope
   companion and an in-scope/shadowing positive.
5. Remove or replace the fallback with active lexical lookup/binding identity.
6. Verify that the primary unknown-name diagnostic makes the expression
   `Unknown` and that `_type_return_value` emits no mismatch cascade.
7. Run focused type-checker scope/binder tests, catch diagnostics, lambda return
   boundary tests, and both full-suite failing e2e cases.

Suggested focused command:

```sh
DRIFT_COMPILER_DEBUG=1 PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/runner.py --summarize catch_binder_scope_leak
```

