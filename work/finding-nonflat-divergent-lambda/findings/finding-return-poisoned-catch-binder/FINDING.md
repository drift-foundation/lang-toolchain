# Finding: out-of-scope catch binder is rebound from function-wide history

Parent: `work/finding-nonflat-divergent-lambda`

Discovered: 2026-08-03 full-suite memcheck codegen e2e gate, after the
shared return-compatibility authority began diagnosing residual mismatches.

Status: open child finding. `PROGRESS.md` is implementer-owned and is
intentionally not created by this review pass.

## Observed

The existing fixture
`lang/tests/codegen/e2e/catch_binder_scope_leak/main.drift` requires the
post-catch `return e` to diagnose `unknown name 'e'`. It now reports only:

```text
return type 'Error' does not match declared type 'Int'
```

The full gate therefore reports:

```text
[codegen e2e] catch_binder_scope_leak: FAIL (missing expected diagnostic)
```

Focused reproduction on the current committed tree is identical.

## Confirmed code path

1. Stage 1 lowers an unresolved name to `HVar(binding_id=None)` after lexical
   lookup fails. The post-catch `e` therefore correctly enters the checker as
   unresolved.
2. While checking the typed catch arm, `type_checker.py` allocates the catch
   binder, adds it to the arm-local `scope_env`/`scope_bindings`, and also
   records it in function-wide `binding_types`/`binding_names` tables.
3. The arm scopes are popped after the catch block.
4. `type_expr(HVar)` first performs the correct lexical-scope searches, but
   then has an old fallback at approximately `type_checker.py:7334` that scans
   every entry in function-wide `binding_names`. It finds the popped catch
   binder by source name, mutates the out-of-scope HVar's `binding_id`, and
   returns its `Error` type.
5. `_type_return_value` now correctly sees a concrete `Error -> Int` mismatch
   and emits that diagnostic. Because the phase-1 checker already failed, the
   later checker path that formerly supplied `unknown name 'e'` is not reached.

The shared return authority exposed the defect; it did not create the stale
name-resolution fallback.

## Proposed patch direction

**Proposed, not authoritative:** remove or lexical-scope-constrain the
function-wide `binding_names.items()` fallback. A function-wide name table is
metadata, not a valid lexical resolver. The same-source-name fallback can also
cross-contaminate sibling scopes and shadowed bindings.

Before deleting it, audit why it was added and enumerate any synthetic or
reconstructed HIR producers that arrive with `binding_id=None`. If legitimate
in-scope HIR currently relies on the fallback, repair that producer or route it
through the active `scope_bindings`; do not retain an out-of-scope resolver.

Do not special-case `Error`, catch binders, or return mismatches, and do not
globally suppress `_type_return_value` after any earlier diagnostic. The
lexical lookup must produce `Unknown` plus its primary unknown-name diagnostic;
the return authority's existing Unknown suppression can then avoid the
secondary return mismatch.

## Relationship to queued causal-poison work

`work/finding-causal-unknown-cascade-suppression` concerns function-global
suppression of diagnostics for unrelated `Unknown` bindings. This child is a
different root cause: an unresolved HVar is incorrectly made concrete by a
function-global name lookup. Keep the two findings separate unless new
evidence proves they share an authority.

## Acceptance criteria

- `catch_binder_scope_leak` again gets the required `unknown name 'e'` and no
  `Error -> Int` return mismatch cascade.
- An in-scope typed catch binder continues to resolve as `Error`, including
  typed-field projection behavior.
- Reusing the same binder name in sibling catch arms remains correct.
- An ordinary block-local used after its lexical scope is likewise rejected as
  unknown rather than rebound through function-wide history.
- Ordinary in-scope locals, parameters, synthetic temporaries, and reconstructed
  HIR still resolve by binding identity or active lexical scope.
- The fix does not rely on global "some prior diagnostic exists" suppression.

## Refactor-trigger result

`doc/refactor_triggers.md` was scanned completely on 2026-08-04 UTC. No
registered trigger matches lexical name resolution or catch-binder scope.

