# Drift spec patch: Numeric Conversion Policy (v1)

## Numeric Conversion Policy (v1)

### 1) Core rule

- Explicit casts are narrowing-permissive.
- `cast<T>(x)` is an explicit, forceful conversion and may truncate/wrap when `T` has smaller range/width.
- The compiler must not reject explicit narrowing casts solely due to overflow risk.

### 2) Integer cast semantics

- For integer-to-integer casts where target width is N bits:
    - result is `x mod 2^N` (low N bits retained).
- Signedness is interpreted by the target type after truncation.
- This applies to both runtime values and compile-time constants under `cast<T>(...)`.

### 3) Float-to-int / int-to-float (v1)

- Keep existing behavior, but document clearly as implementation-defined for edge cases unless already pinned.
- Add tests for current behavior so it is stable per target.

### 4) Literal policy remains strict

- Typed literals (for example `123u`) and `const` declarations keep strict range validation for the declared type.
- This is separate from cast semantics:
    - `const x: Uint = 184467...u` -> reject if out of range.
    - `const x: Uint = cast<Uint>(...)` -> allowed truncation per cast rule *(once const-expr evaluation supports cast; currently v1 rejects non-literal const initializers)*.

### 5) Checked conversions (safe API)

- Provide checked conversions as explicit APIs (separate from cast), e.g. in `std.num`:
    - `to_uint_checked(x: Int) -> Result<Uint, ConversionError>`
    - `to_uint64_checked(x: Int) -> Result<Uint64, ConversionError>`
    - `to_int_checked(x: Uint) -> Result<Int, ConversionError>`
- Checked APIs must fail on out-of-range instead of truncating.

### 6) Diagnostics policy

- No overflow diagnostic for explicit `cast<T>(...)` narrowing.
- Keep diagnostics for:
    - invalid cast shape/type category,
    - invalid typed literal ranges,
    - invalid checked conversion calls (if provided).

### 7) Regression requirements

Add/keep tests that pin:

1. `cast<Uint>(2^32 + 7)` truncation behavior (target-specific expected value: 7 on 32-bit, 4294967303 on 64-bit).
2. `u`-suffix overflow rejection.
3. local/module `const` strict literal range rejection.
4. checked conversion reject path once API is added.
