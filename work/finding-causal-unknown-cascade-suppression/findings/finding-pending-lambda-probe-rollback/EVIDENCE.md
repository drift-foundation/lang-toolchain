# Evidence: pending-lambda mutation across deferred-probe rollback

Snapshot: 2026-08-04, pending `0.35.0` tree.

## Static facts

- `lang/driftc/checker/call_resolver.py`'s `_DEFER_PROBE_SAFE_NODES` includes
  `HVar` and `HCall`.
- The NEEDS_EXPECTED path begins `CheckerStateTxn`, recursively resolves the
  candidate expression, then calls `rollback()` when the result remains
  Unknown/silently expected-dependent.
- `lang/driftc/type_checker.py` resolves a pending HCall callee before entering
  its nested `resolve_call_expr`: it types the stored HLambda, overwrites
  `binding_types`, and pops `pending_lambda_by_binding`.
- Neither plain dict belongs to `FnCheckState.OWNED_TABLES`.
- `CheckerStateTxn` snapshots only the passed probe subtree. The stored HLambda
  initializer referenced by a callee HVar is elsewhere in the function body,
  not a child of the probed HCall.

Together these facts contradict `FnCheckState`'s documented safe-shape
invariant whenever a deferred probe subtree references a pending lambda.

## Executed work-only evidence from K

`probe_txn_and_value_positions.py::test_probe_txn_generic_arg_pending_call`
compiled:

```drift
fn id<T>(x: T) nothrow -> T { return x; }

pub fn main() nothrow -> Int {
	val f = || => { 7 };
	val r = id(f());
	return r - 7;
}
```

Observed aggregate resolver delta:

```text
probes: 57
commits_complete: 57
rollbacks: 0
errors: 0
```

This proves live probes and a clean accepted control. It does not identify the
exact nested transaction or exercise rollback after the pending mutation.

## Required forcing probe

Construct one outer deferred safe expression containing, in evaluation order:

- `f()` through a pending stored lambda;
- a call known to force NEEDS_EXPECTED, based on the existing
  `dflt<T>() -> Array<T>` transaction test.

A generic helper with two arguments is a likely vehicle, used as the argument
to a context-providing outer method/call. Install the same independent frame-
state audit used by `test_defer_probe_state_transaction.py` and require at
least one audited rollback. Before the fix, the audit should name the pending
map and/or binding-type mismatch; after the fix, the dynamic gate should keep
that candidate from opening a transaction.

Do not settle for aggregate probe counts: instrument the candidate expression
or claimant binding id so the evidence links the rollback to the pending
mutation.

## Suspected affected boundary

- `lang/driftc/checker/call_resolver.py`: dynamic safe-shape decision and
  `CallResolverContext` predicate.
- `lang/driftc/type_checker.py`: pending-binding predicate wiring at all
  `make_call_ctx(...)` sites.
- a new focused test file preferred; editing the existing transaction test
  requires explicit human approval.

No lowering/runtime/stdlib/spec file is currently implicated.
