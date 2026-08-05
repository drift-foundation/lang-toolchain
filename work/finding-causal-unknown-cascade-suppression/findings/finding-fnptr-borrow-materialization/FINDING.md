# Child finding: named function-pointer borrow violates canonical place shape

Date filed: 2026-08-05

Parent: `finding-causal-unknown-cascade-suppression`.

Status: confirmed `LANGUAGE_BUG`, proposed fold-in because pending-HVar value
finalization needs the same function-pointer borrow boundary. Shared edits are
blocked on the parent full-suite and planning gates.

## Reproducer and control

Accepted control:

```drift
val f = || => { 7 };
val _ = f();
val r = &f;
```

This full-compiles and runs, establishing that a shared borrow of a stored,
finalized thin function pointer is legal.

Failing named-function form:

```drift
fn seven() nothrow -> Int { return 7; }
val r = &seven;
```

The compiler raises raw `AttributeError: 'HFnPtrConst' object has no attribute
'name'`. A Python traceback is never an acceptable user outcome.

## Leading mechanism, not yet authority

Stage1 borrow materialization is syntactic and treats HVar as a place. Later,
semantic function-reference resolution records a fnptr replacement for that
same node. `_apply_fnptr_consts` recursively rewrites marked HNodes, including
an `HPlaceExpr.base`, to HFnPtrConst. That conflicts with the documented
canonical-place invariant that the base is always HVar and plausibly explains
the `.name` crash.

A structural red test must capture the exact pre/post replacement shape and
traceback site. The implementer should replace this hypothesis if the trace
disproves it.

## Required outcome

- `&named_function` and pending/finalized stored-fnptr borrows have one
  deliberate accepted representation and full compile/run coverage;
- a function constant borrowed as an rvalue receives real temporary storage,
  or an equivalent existing lowering-visible representation;
- `HPlaceExpr.base` remains within its HVar-only contract;
- no AttributeError catch, arbitrary-rvalue place expansion, or checker-only
  type relabeling;
- existing invalid mutable/rvalue borrow diagnostics remain clean;
- no spec or ABI change.
