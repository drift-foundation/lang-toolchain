# Evidence: `bitwise_uint_ops`

## Full-suite observation

```text
[codegen e2e] bitwise_uint_ops: FAIL (unexpected checker diagnostics)
```

## Focused diagnostic

With `DRIFT_COMPILER_DEBUG=1`, the focused runner reports:

```text
bitwise_uint_ops: FAIL (unexpected checker diagnostics: return type 'Uint' does not match declared type 'Int')
```

## Relevant source shape

```drift
pub fn main() nothrow -> Int {
    var x: Uint = 6;
    // Uint bitwise operations ...
    return x;
}
```

The fixture predates the shared residual return-mismatch diagnostic. Its blame
history shows the final `return x` has existed since the original bitwise test.

