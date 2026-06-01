# Spec change request: checked division/modulo and nothrow interaction

## Summary

Pin the language semantics for integer division (`/`) and modulo (`%`) by zero under the Drift error model (Model A):

- **Division/modulo by zero throws a language error** (it is not a trap and not UB).
- Therefore `/` and `%` are **may-throw operations**.
- In a `nothrow` function, `/` and `%` are rejected unless the compiler can prove the divisor is nonzero.

Pinned implementation decision (for clean integration):

- Treat `/` and `%` exactly like calls to a compiler-known **intrinsic** whose `CallSig.can_throw = true`.
- This makes `nothrow` enforcement, CallInfo propagation, boundary/wrapper policy, and diagnostics consistent with existing “may throw” machinery.

This document describes the required spec edits and the intended compiler model.

---

## Motivation

Today, `nothrow` is meaningful only if the language clearly defines which operations can throw implicitly (without an explicit `throw`). Arithmetic divide/mod by zero is the most common implicit error source and must be pinned to avoid:

- ambiguous semantics between “throws” vs “traps”,
- backend-dependent behavior,
- inconsistent `nothrow` diagnostics,
- future refactors breaking invariants (“no missing CallInfo” / ABI correctness).

Model A (“throws”) is the safer, production-grade default.

---

## Proposed semantics

### Integer division and modulo

For integer types (e.g., `Int`, `UInt`, and any fixed-width signed/unsigned integer types):

- `a / b`:
  - if `b == 0`, the operation throws a language error.
- `a % b`:
  - if `b == 0`, the operation throws a language error.

This applies regardless of sign.

#### Notes
- This spec change does not pin overflow behavior (e.g., `MIN_INT / -1`) unless already specified elsewhere. If overflow semantics are not pinned yet, document them separately.

---

## Effect on `nothrow`

### Rule

A function declared `nothrow` must not contain any operation that **may throw**, directly or indirectly.

Because `/` and `%` may throw:
- `nothrow` functions that contain `/` or `%` must be rejected **unless the divisor is provably nonzero**.

### Provably nonzero (conservative acceptance)

The compiler may accept division/modulo in a `nothrow` function only when it can prove the divisor is nonzero. The proof system is intentionally conservative; the compiler is allowed to reject cases that are safe but not provable.

Minimum set of accepted proofs:

1. **Literal nonzero divisor**
   - `a / 1`, `a % 7`, etc.

2. **Dominating guard check**
   - The compiler recognizes patterns such as:
     - `if b == 0 { ... } else { a / b }`
     - `if b != 0 { a / b } else { ... }`
   - And similar structures where the “nonzero” condition dominates the division/modulo.

3. **Compiler-known facts**
   - If the compiler proves `b` is nonzero from prior checks or invariants, it may accept.

The spec should explicitly allow the compiler to grow this set over time without breaking compatibility.

---

## Error model and error type

Division/modulo by zero throws a language error that participates in the normal exception/error mechanism.

This document does not require a particular concrete error type name. Acceptable options include:
- a generic `Error`,
- a named `ArithmeticError`,
- or a structured error code within the existing error system.

The key requirement is: it is treated as a **throwing operation** at the language level.

---

## Pinned compilation model (intrinsic-based)

To integrate cleanly with existing `nothrow` and CallInfo/ABI machinery:

### Operator lowering model

Lower `/` and `%` to compiler-known intrinsic calls:

- `intrinsic.int_div(a, b) -> Int` with `CallSig.can_throw = true`
- `intrinsic.int_mod(a, b) -> Int` with `CallSig.can_throw = true`

(Names illustrative; actual intrinsic naming is an implementation detail.)

### Consequences

- `/` and `%` participate in:
  - `CallInfo` generation
  - `may_throw` analysis
  - `nothrow` enforcement
  - wrapper decisions / ABI selection (when applicable)

### Optimization allowance

When the compiler proves the divisor is nonzero, it may lower to a non-throwing operation or intrinsic:
- e.g., `intrinsic.int_div_nz(a, b) -> Int` with `can_throw = false`

This is an optimization and does not change surface semantics.

---

## Diagnostics requirements

### In `nothrow` functions

If a division/modulo is not provably nonzero, emit an error:

- “division by zero may throw in `nothrow` function”
- or “operation may throw in `nothrow` function: divisor may be zero”

The diagnostic should point to the divisor expression (or the operator span) and should be consistent with other “may throw” diagnostics.

### In non-`nothrow` functions

No special diagnostic is required; the operation is simply may-throw and must be handled according to whatever error-handling rules apply (try/catch/propagate, etc.).

---

## Spec edits to apply

### 1) Operators / arithmetic section
Add a subsection under integer arithmetic:

- “Checked division and modulo”
- define `/` and `%` throwing behavior on divisor `0`
- mention may-throw classification

### 2) `nothrow` section
Add an explicit clause:

- “Some operators may throw; division and modulo are defined as may-throw.”
- define “provably nonzero” and conservative proof allowance

### 3) Error/exception section
Add a short bullet:

- “Implicit runtime errors exist; division/modulo by zero is one such error.”

### 4) (Optional) Intrinsics section (if present in spec)
If the spec already documents intrinsics, add:

- `/` and `%` are specified as if implemented via can-throw intrinsics.

If the spec does not have an intrinsics section, keep the intrinsic model as “compiler model note” (non-normative) but still pinned as an implementation decision for Drift.

---

## Non-goals

- This change does not define float division semantics (NaN/Inf behavior) unless already pinned.
- This change does not define overflow behavior.
- This change does not introduce new syntax.

---

## Future work

### Explicit checked/unchecked variants
To support performance-sensitive code, consider standard library functions:

- `checked_div(a, b) -> Result<Int>` (or equivalent)
- `unchecked_div(a, b) -> Int` (may trap/UB; explicitly outside the “throwing” model)
- similarly for modulo

No commitment required now, but reserving names and intent prevents future ambiguity.

### Enhanced proof rules
The compiler may expand “provably nonzero” reasoning over time:
- range analysis
- value refinement types
- constraint propagation

This should remain conservative and must not change runtime semantics.

---

## Compatibility impact

- Programs that previously assumed divide-by-zero traps/UB must now treat it as a thrown error.
- `nothrow` functions containing `/` or `%` may begin failing compilation unless they guard the divisor or the compiler can prove nonzero.

This is intended and improves semantic clarity and correctness.
