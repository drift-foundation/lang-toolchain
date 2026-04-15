# Array literal with Copy non-bitcopy struct element fails MIR invariant

## Status
**FIXED in 0.27.193.**  `_visit_expr_HArrayLiteral` now (1) calls
`_copy_if_ref_alias` before the per-element CopyValue (mirroring
the standard ownership-boundary upgrade used elsewhere in the
lowering), and (2) gates the CopyValue on
`copy_status is True and not is_bitcopy(val_ty)` (matching
`_ensure_array_elem_copy`) instead of the narrower
`_should_copy_value`, with a paired DropValue when the element
source is an OWNED rvalue temp (HVar / projection-free
HPlaceExpr stays bound).

Originally exposed by the ownership-transfer matrix
(lang/tests/codegen/e2e/__ownership_matrix__/_gen.py) on
2026-04-14; matrix re-enabled `om_array_literal_diag_entry`
which now passes plain + memcheck + ASAN.

(Pre-fix description retained below for archival reference.)

## Symptom

`val arr: Array<T> = [v];` where `T` is a Copy non-bitcopy struct
(e.g. `core.DiagnosticEntry`) and `v` is any expression (HVar,
HCall rvalue, projection) fails MIR validation:

```
MIR invariant violation: array store in m::scenario_X must use
CopyValue or MoveOut for Copy element type
```

Array literal with `String` elements does NOT trip the invariant —
the emitter handles the scalar-String case via `string_arc` but not
the aggregate-struct case.

## Relation to 0.27.39-dev / 0.27.192

0.27.39-dev added `_ensure_array_elem_copy` to satisfy the array-
store CopyValue/MoveOut invariant at `Array.push/insert/set/extend`
sites.  That fix intentionally did not touch the `ArrayLit`
lowering path — which is what this bug reports.  The fix shape
for ArrayLit is the same: emit `CopyValue` (with a matching
`DropValue` only when the element source is an owned temp; see
`_call_arg_yields_owned_temp` and 0.27.192) around each element
before the array-slot store.

## Repro

Post-fix fixture: `lang/tests/codegen/e2e/om_array_literal_diag_entry/`
(no longer in `KNOWN_SKIP_COMBOS`).  Run via
`PYTHONPATH=. ./.venv/bin/python lang/tests/codegen/e2e/runner.py om_array_literal_diag_entry`
under plain, `DRIFT_MEMCHECK=1`, and `DRIFT_ASAN=1`.

Minimal Drift:
```drift
module m;
import std.core as core;

pub fn main() nothrow -> Int {
    val src: core.DiagnosticEntry = core.diagnostic_entry("k", DiagnosticValue::String("v"));
    val arr: Array<core.DiagnosticEntry> = [src];
    return 0;
}
```

## Suspected subsystem
- `lang/driftc/stage2/hir_to_mir.py` — `ArrayLit` lowering
  (search for `M.ArrayLit` or `_visit_expr_HArrayLit`).

## Scope
MIR lowering change only, no ABI bump.
