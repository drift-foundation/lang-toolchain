# Evidence: out-of-scope catch binder

## Full-suite observation

```text
[codegen e2e] catch_binder_scope_leak: FAIL (missing expected diagnostic)
```

## Focused diagnostic

```text
catch_binder_scope_leak: FAIL (missing expected diagnostic: return type 'Error' does not match declared type 'Int')
```

## Minimal existing source

```drift
try {
    val _ = fail();
} catch EvTest(e) {
    val moved = move e;
} catch {
}
return e;
```

The existing expected contract is `unknown name 'e'` in the typecheck phase.

## Resolver evidence

After active lexical-scope lookup fails, the HVar branch contains this
function-wide fallback shape:

```python
if expr.binding_id is None and binding_names:
    for bid, name in binding_names.items():
        if name == expr.name:
            expr.binding_id = bid
            ...
            return record_expr(expr, binding_types.get(bid, self._unknown))
```

For this fixture, the only historical binding named `e` is the already-popped
catch binder, whose recorded type is `Error`.

