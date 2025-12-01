# Drift spec patch: native numeric types, `Size`, FFI mapping, and `std.bits`

## 1. Update primitive palette (Chapter 3.1)

### 1.1. Replace the numeric portion of the table in §3.1

**Current (partial) excerpt** in §3.1:

> | Type                 | Description                                                                   |
> | -------------------- | ----------------------------------------------------------------------------- |
> | `Bool`               | Logical true/false.                                                           |
> | `Int64`, `UInt64`, … | Fixed-width signed/unsigned integers.                                         |
> | `Float64`, `Float32` | IEEE-754 floating point.                                                      |
> | `Byte`               | Unsigned 8-bit value (`UInt8` under the hood); used for byte buffers and FFI. |
> | `String`             | UTF-8 immutable rope.                                                         |

**Replace with:**

```markdown
### 3.1. Primitive palette (updated)

| Type    | Description |
|---------|-------------|
| `Bool`  | Logical true/false. |
| `Int`   | Signed two’s-complement integer of the platform’s natural word size. Guaranteed to be at least 32 bits. |
| `Uint`  | Unsigned integer of the platform’s natural word size. Same bit-width as `Int`. |
| `Size`  | Unsigned integer used for lengths, indices, and offsets. Guaranteed to be at least 16 bits and at least as wide as a pointer on the target. |
| `Float` | IEEE-754 binary floating type used as the default floating-point scalar. Guaranteed to be at least 32 bits. Implementations must document whether `Float` is `F32` or `F64` on a given target. |
| `Int8`, `Int16`, `Int32`, `Int64` | Fixed-width signed integers, exactly 8/16/32/64-bit two’s-complement. |
| `Uint8`, `Uint16`, `Uint32`, `Uint64` | Fixed-width unsigned integers, exactly 8/16/32/64-bit. |
| `F32`, `F64` | IEEE-754 binary32 and binary64 floating-point types. |
| `Byte` | Unsigned 8-bit value (`Uint8` under the hood); used for byte buffers and FFI. |
| `String` | UTF-8 immutable rope. |
```

### 1.2. Add a new subsection: “3.1.x Integer and float semantics”

Right after the updated table, add:

```markdown
#### 3.1.x Integer and float semantics

Drift distinguishes between **natural-width** numeric primitives and **fixed-width** primitives.

- Natural-width primitives (`Int`, `Uint`, `Size`, `Float`) map to the platform’s “obvious” efficient scalars:
  - `Int` / `Uint` are at least 32 bits and typically match the native register size (e.g., 32-bit on 32-bit targets, 64-bit on 64-bit targets).
  - `Size` is at least 16 bits and always at least as wide as a pointer on the target; it is used for container lengths, indices, and offsets.
  - `Float` is either `F32` or `F64`; implementations must document which one they choose on each target.

- Fixed-width primitives (`Int8`/`Uint8` … `Int64`/`Uint64`, `F32`, `F64`) have **exact bit-widths** and are used for:
  - binary formats and wire protocols,
  - on-disk representations,
  - narrow interfaces to foreign code that specify exact C widths.

##### Overflow behavior

- For **fixed-width integer types** (`Int8`…`Int64`, `Uint8`…`Uint64`), arithmetic uses modular two’s-complement behavior: results wrap on overflow.
- For **natural-width integer types** (`Int`, `Uint`, `Size`):
  - In debug builds, implementations **should** trap on overflow to aid early detection.
  - In optimized/release builds, arithmetic **may** use modular wrapping, matching the fixed-width types, unless the implementation guarantees trapping semantics.
  - Implementations may provide checked arithmetic helpers (`checked_add`, `checked_sub`, etc.) in the standard library for code that needs explicit overflow handling.

##### Conversions

- Widening conversions that cannot overflow (e.g., `Int32` → `Int64`, `Uint32` → `Uint64`) may be implicit.
- Narrowing or sign-changing conversions (e.g., `Int64` → `Int32`, `Uint32` → `Int32`, `Uint` → `Size` when `Uint` is wider) **must be explicit** and may fail at runtime if the value is out of range.
- Conversions between `Size` and other integer types follow the same rules:
  - `Size` → `Uint` is always lossless.
  - `Uint` → `Size` is only lossless if `bits(Uint) ≤ bits(Size)`; otherwise, it must use an explicit, potentially checked cast.
  - `Int` → `Size` requires an explicit cast and is only well-defined for non-negative values.

Floating-point conversions between `Float`, `F32`, and `F64` follow IEEE-754 rules; narrowing conversions (`F64` → `F32`) must be explicit.
```

---

## 2. Clarify `Size` usage across the spec

The spec already talks about array lengths, indices, capacities, etc., but mostly uses `Int`/`Int64` in examples.

You don’t have to rewrite every example immediately, but you should add **one clear rule** early and then treat existing `Int64` usages as “illustrative” until you sweep them.

In §12 (arrays) and any place that defines `Array`, `ByteBuffer`, and slices, add a short rule:

```markdown
**Indexing and lengths.** All container lengths, capacities, and indices use `Size`:

- `Array<T>.len: Size`
- `Array<T>.capacity: Size`
- `ByteBuffer.len: Size`
- `ByteSlice.len: Size`

Any function that indexes into a container or string must accept a `Size` (or a value explicitly convertible to `Size` without narrowing). Examples in this document that show `Int64` for lengths or indices are illustrative only; the canonical type is `Size`.
```

You can drop that paragraph either:

* at the end of §12.1 (ByteBuffer, ByteSlice, MutByteSlice), and/or
* right after the Array layout in §16.5.2, to tie it into the memory model.

---

## 3. FFI & numeric mapping rules (ABI chapter)

Add a dedicated subsection under the ABI / pointer-free / `lang.abi` area (around §17.5). Title suggestion:

### 3.1. New subsection: “Numeric types in FFI”

````markdown
#### 17.x Numeric types in FFI

Drift distinguishes **natural-width** and **fixed-width** numeric primitives. FFI bindings must respect how C expresses numeric widths:

1. **C uses implementation-defined integer types**  
   (e.g., `int`, `unsigned`, `size_t`, `ptrdiff_t`, `uintptr_t`):
   - Drift bindings may use the corresponding natural-width primitives:
     - `size_t` → `Size`
     - `ptrdiff_t` → `Int`
     - `uintptr_t` → `Uint`
     - `int` → `Int` (or `Int32` if the ABI explicitly freezes C `int` to 32-bit)
   - This pattern is appropriate when the C API itself is intentionally abstract over width.

2. **C uses explicit fixed widths (`<stdint.h>` / `<inttypes.h>`)**  
   (e.g., `int16_t`, `uint32_t`, `uint8_t`):
   - Drift bindings **must** use the matching fixed-width primitives:
     - `int8_t` → `Int8`
     - `int16_t` → `Int16`
     - `int32_t` → `Int32`
     - `int64_t` → `Int64`
     - `uint8_t` → `Uint8`
     - `uint16_t` → `Uint16`
     - `uint32_t` → `Uint32`
     - `uint64_t` → `Uint64`
   - Natural-width primitives (`Int`, `Uint`, `Size`, `Float`) **must not** appear in such signatures.

3. **`Size` ABI representation**

   At the C and LLVM boundary, `Size` lowers to an unsigned integer type that is at least as wide as a pointer and at least 16 bits. Implementations typically map `Size` to `uintptr_t` in C shims:

   ```c
   typedef uintptr_t DriftSize;
````

Drift’s `Size` is represented as `DriftSize` in C structs and function signatures.

4. **Public APIs vs. FFI surface**

   * FFI modules under `lang.abi.*` mirror C headers exactly and therefore use fixed-width Drift primitives whenever the C API does.
   * Public Drift APIs in `std.*` and user code should prefer the natural-width types (`Int`, `Uint`, `Size`, `Float`, domain types) and hide fixed widths behind wrappers.
   * Narrowing conversions (for example, `Size` → `Uint32` for a `uint32_t len` parameter) must be explicit and should be checked or documented.

This separation keeps FFI bindings precise while preventing fixed-width types from leaking unnecessarily into everyday user code.

````

---

## 4. Public pattern for wrapping fixed-width C APIs

This is more guidance than semantics, but it belongs either:

- at the end of the new §17.x, or  
- in a short subsection “FFI wrapper pattern”.

```markdown
##### 17.x.y FFI wrapper pattern

For C APIs that use explicit fixed widths (e.g., `uint32_t len`), the recommended pattern is:

- Define the low-level binding in `lang.abi.*` using fixed-width types:

  ```drift
  // lang.abi.zlib

  extern "C"
  fn crc32(seed: Uint32, data: ref Uint8, len: Uint32) returns Uint32
````

* Provide a higher-level wrapper in a public module (e.g., `std.zlib`) that uses `Size`, `String`, or container types:

  ```drift
  // std.zlib

  fn narrow_size_to_u32(len: Size) returns Uint32 {
      if len > Uint32::MAX {
          throw Error("len-too-large", code = "zlib.len.out_of_range")
      }
      return cast(len)
  }

  fn crc32(seed: Uint32, buf: ByteBuffer) returns Uint32 {
      val len32: Uint32 = narrow_size_to_u32(buf.len())
      return lang.abi.zlib.crc32(seed, buf.as_slice().data_ptr(), len32)
  }
  ```

User code imports only the higher-level wrapper; the fixed-width details remain localized to the FFI layer.

````

---

## 5. `std.bits` module (bitmasks) – new section

Since `std.bits` isn’t in the spec yet, add a short chapter near the end of the “standard library surface” material (after I/O or concurrency). Something like **“23. Bitmask helpers (`std.bits`)”**:

```markdown
## 23. Bitmask helpers (`std.bits`)

Bitmasks are one of the few common cases where explicit integer widths matter in user-level code. To keep intent clear without pushing raw `Uint32` everywhere, the standard library provides a small `std.bits` module.

### 23.1 Types

`std.bits` defines a set of aliases for unsigned integer types:

```drift
module std.bits

// Natural-width mask: same width as Uint
type Bits = Uint

// Explicit-width masks
type Bits8  = Uint8
type Bits16 = Uint16
type Bits32 = Uint32
type Bits64 = Uint64
````

These aliases introduce intent (`Bits32` as “32-bit bitmask”) without changing layout or performance.

### 23.2. Bitmask helpers

`std.bits` exposes simple helper functions for common operations:

```drift
fn set(mask: Bits32, flags: Bits32) returns Bits32
fn clear(mask: Bits32, flags: Bits32) returns Bits32
fn toggle(mask: Bits32, flags: Bits32) returns Bits32

fn has_all(mask: Bits32, flags: Bits32) returns Bool
fn has_any(mask: Bits32, flags: Bits32) returns Bool
```

Implementations are straightforward wrappers over bitwise operators:

* `set(m, f)`    ≡ `m | f`
* `clear(m, f)`  ≡ `m & ~f`
* `toggle(m, f)` ≡ `m ^ f`
* `has_all(m,f)` ≡ `(m & f) == f`
* `has_any(m,f)` ≡ `(m & f) != 0`

Generic versions may be provided via a `BitMask<T>` trait implemented for `Uint8/16/32/64` and `Uint`, but the core surface does not require users to interact with the trait directly.

### 23.3. Prelude policy

`std.bits` is **not** imported by the prelude. Code that uses bitmasks must opt in explicitly:

```drift
import std.bits

fn has_read_permission(perms: Bits32) returns Bool {
    return bits.has_any(perms, READ)
}
```

Standard library modules that internally use bitmasks (e.g., for file mode flags or protocol parsing) are free to depend on `std.bits` but should avoid exposing raw bitmasks in their public APIs unless the fixed width is part of the domain.

```

---

## 6. Minor cleanups and cross-references

These are small but keep the story coherent:

1. **Anywhere the spec calls out `Int64` as “the” integer** (e.g., in examples or in wording like “`Int64` is the default integer”):

   - Replace that phrasing with “`Int` is the default integer type”.
   - Leave explicit `Int64` uses that are clearly examples or domain-specific.

2. **Where `Int64` is used for lengths or indices** (e.g., `fn len(self: &ByteBuffer) returns Int64`):

   - Update signatures to return `Size` instead of `Int64`.
   - If you don’t want to churn all the code snippets at once, at least update the *normative* text as in §2 above so it’s clear that `Size` is the canonical type.

3. **Where floats are mentioned in a generic way** (e.g., “`Float64` is the default”):

   - Replace with “`Float` is the default floating-point type. Implementations must document whether `Float` is `F32` or `F64` on a given target.”
