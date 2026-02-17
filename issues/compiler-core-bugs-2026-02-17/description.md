# Compiler Bug Batch (2026-02-17)

This report tracks recent compiler/toolchain defects and regressions observed during user-land package work (mariadb client + runtime/leak validation sweeps).

## 1) Internal type-id leak in user diagnostic (`type 170, expected 1`)

Severity: High (diagnostic quality / debuggability)

Observed diagnostic shape:

- `argument 1 to lang.__intrinsic::__method::push has type 170, expected 1`
- Missing actionable source location (`<source>:None:None` in some runs)

Why this is bad:

- Exposes internal numeric type IDs instead of language type names.
- Missing source location makes the error hard to act on.

Expected:

- Human-readable types in diagnostics (e.g. `ResultSetCell`, `Byte`, `Array<ResultSetCell>`, etc.).
- Concrete file/line/column for user-facing errors.

Likely subsystem:

- Checker/type error rendering for intrinsic method dispatch (`push`) path.
- Diagnostic span propagation for intrinsic call mismatch paths.

Regression test target:

- Driver/e2e case that forces intrinsic arg mismatch and asserts:
  - symbolic type names in message,
  - populated file/line/column.

---

## 2) Variant lowering contract failure leaked as internal diagnostic

Severity: High (CORE_BUG)

Observed diagnostic:

- `internal: LLVM lowering contract failure (internal: variant 'ResultSetCell' missing tombstone_ctor)`

Why this is bad:

- Internal lowering invariant leaked to user.
- Indicates variant tombstone metadata path was incomplete/inconsistent for some shapes.

Expected:

- Valid program compiles without internal diagnostics.
- Unsupported shape (if any) should fail earlier with checker-phase user diagnostic + span.

Likely subsystem:

- Variant metadata generation and LLVM tombstone emission contract.

Status:

- Recent fix landed to centralize tombstone ctor resolution and use instantiated/internal tombstone metadata.
- Verification sweep completed across core/package/LLVM/e2e tombstone paths; no regression observed.
- Marked resolved with regression coverage in place.

Regression test target:

- e2e: droppable variant without explicit `@tombstone` through array-pop/take path.
- LLVM unit: internal synthesized tombstone tag path.

---

## 3) Non-Copy indexing crash history (now checker diagnostic) needs permanent guard

Severity: Medium-High (CORE_BUG history)

Previous failure mode:

- Internal exception on array index read for non-Copy element type.

Current status:

- Crash path converted to checker diagnostic (`cannot copy value of type ...`).
- Added driver boundary regressions to assert this remains a user-facing typecheck diagnostic with populated span and no `internal:` leakage.

Remaining risk:

- Regression risk at checker/stage2 boundary if index handling paths diverge.

Expected:

- Deterministic checker error for illegal non-Copy index value-read patterns.
- No internal exceptions/tracebacks.

Likely subsystem:

- Ownership rules + HIR/MIR lowering boundary for `HIndex`.

Regression test target:

- e2e + driver assertions for non-Copy `arr[i]` value-read rejection with source span.

Status:

- Resolved/pinned for current pipeline with:
  - e2e rejection case (`array_index_non_copy_read_rejected`)
  - new driver span/non-internal guards (`test_array_index_noncopy_diagnostics.py`)

---

## 4) Checker internal error from enum drift (`TypeKind.TYPE_PARAM`)

Severity: High (CORE_BUG)

Observed traceback:

- `AttributeError: type object 'TypeKind' has no attribute 'TYPE_PARAM'`
- In checker generic/type-param scan helper.

Why this is bad:

- Internal exception instead of user diagnostic.
- Indicates fragile enum coupling across checker paths.

Expected:

- No internal exceptions from type-kind checks.
- Consistent type-param detection independent of enum-name drift.

Likely subsystem:

- Checker type-kind handling / generic-type scanning utilities.

Status:

- Local fix applied (`TYPEVAR` path).
- Added driver regression coverage for generic index/typevar scan paths to ensure no internal checker exception and stable user-facing diagnostics.
- Marked resolved/pinned for current pipeline.

Regression test target:

- Driver-level generic index/read/type-param scenarios that execute this scan path.

---

## 5) Byte const support edge follow-up (cross-module + intrinsic integration)

Severity: Medium

Context:

- `const A: Byte = 12` support was added and validated.
- Follow-up failures in nearby paths suggested integration/diagnostic seams can still be confusing when Byte constants flow into intrinsic methods.

Expected:

- Byte const behaves as a first-class typed literal across modules and intrinsic calls.
- Any failures should show symbolic type names + source spans.

Likely subsystem:

- Const typing + intrinsic method resolution + diagnostic rendering integration.

Regression test target:

- Cross-module Byte const usage in intrinsic call contexts (`push`/array ops) with span/type-message assertions.

---

## Standard report template (for each concrete bug)

1. Title
2. Minimal repro file
3. Command
4. Actual output
5. Expected output
6. Suspected subsystem
7. Regression test path to add
