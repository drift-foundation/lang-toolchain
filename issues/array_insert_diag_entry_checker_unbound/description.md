# `Array<core.DiagnosticEntry>.insert(idx, HVar)` — checker UnboundLocalError

## Status
**FIXED in 0.27.193.**  `call_resolver.py:1782-1801` had two early-
return-on-OK branches in `insert`'s arg-type validation that
referenced an `info` symbol built only at the success exit at
~line 1833.  Replaced with `pass` (mirroring the `push`/`set`
branch) so control falls through to the unified info-builder.

Originally exposed by the ownership-transfer matrix
(lang/tests/codegen/e2e/__ownership_matrix__/_gen.py) on
2026-04-14; matrix re-enabled `om_array_insert_diag_entry`
which now passes plain + memcheck.

(Pre-fix description retained below for archival reference.)

## Symptom

Calling `arr.insert(idx, val)` on `Array<core.DiagnosticEntry>` where
`val` is an HVar-bound local (or projected place) crashes the
type-checker with a Python-level `UnboundLocalError`:

```
File "lang/driftc/checker/call_resolver.py", line 1793
    return MethodCallResult(recv_nominal, info)
                                          ^^^^
UnboundLocalError: cannot access local variable 'info' where it is
not associated with a value
```

The same `.insert(idx, HCall rvalue)` shape compiles cleanly, so the
failing path is specific to the non-HCall argument shape reaching
the resolver after an early-return branch that skipped `info`'s
assignment.

Not reproducible on `Array<String>.insert(...)` — element-type
matters.

## Repro

Post-fix fixture: `lang/tests/codegen/e2e/om_array_insert_diag_entry/`
(no longer in `KNOWN_SKIP_COMBOS`).  Run via
`PYTHONPATH=. ./.venv/bin/python lang/tests/codegen/e2e/runner.py om_array_insert_diag_entry`
under plain and `DRIFT_MEMCHECK=1`.

Minimal Drift:
```drift
module m;
import std.core as core;

pub fn main() nothrow -> Int {
    var arr: Array<core.DiagnosticEntry> = [];
    val v = core.diagnostic_entry("k", DiagnosticValue::String("v"));
    arr.insert(0, v);   // <-- crashes checker
    return 0;
}
```

## Suspected subsystem
- `lang/driftc/checker/call_resolver.py:1793` and the code path that
  leaves `info` unbound.  Likely an early-return branch in the
  array-intrinsic-method resolver that skipped the `info = ...`
  assignment prior to the unconditional `return MethodCallResult(...)`.

## Scope
Checker-only fix; no ABI bump.
