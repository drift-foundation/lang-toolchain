# `drift-lang-abi.md`

### **Drift Language ABI — Scalars, Errors, Events, FnResult, Calling Conventions (v1)**

**Purpose:**
This document defines the **binary interoperability ABI** for Drift programs across module boundaries and for C/LLVM interop.
It covers:

* Scalar numeric / boolean types
* The `Error` type
* Exception event codes (their full bit layout)
* `FnResult<T, Error>` representation
* Calling conventions for internal vs. exported Drift functions
* C/LLVM IR equivalents

---

# 1. Scalar ABI

Drift has two classes of scalars:

* **Natural width primitives:** fixed as 64-bit for ABI stability
* **Fixed-width primitives:** identical to C/LLVM integer/float widths

### 1.1 Natural-width primitives

| Drift type | C ABI type           | LLVM IR                           | Notes                                  |
| ---------- | -------------------- | --------------------------------- | -------------------------------------- |
| `Int`      | `int64_t`            | `i64`                             | Signed integer.                        |
| `Uint`     | `uint64_t`           | `i64`                             | Unsigned.                              |
| `Size`     | `uintptr_t` (64-bit) | `i64`                             | Pointer-sized, pinned to 64-bit in v1. |
| `Bool`     | `uint8_t`            | `i1` (in regs), `i8` (in structs) | ABI defines on-wire as 1 byte.         |
| `Float`    | `double`             | `double`                          | IEEE-754 64-bit float.                 |

### 1.2 Fixed-width primitives

| Drift    | C ABI      | LLVM     |
| -------- | ---------- | -------- |
| `Int8`   | `int8_t`   | `i8`     |
| `Int16`  | `int16_t`  | `i16`    |
| `Int32`  | `int32_t`  | `i32`    |
| `Int64`  | `int64_t`  | `i64`    |
| `Uint8`  | `uint8_t`  | `i8`     |
| `Uint16` | `uint16_t` | `i16`    |
| `Uint32` | `uint32_t` | `i32`    |
| `Uint64` | `uint64_t` | `i64`    |
| `F32`    | `float`    | `float`  |
| `F64`    | `double`   | `double` |

---

# 2. Error ABI

`Error` is a structured error object capturing:

* A 64-bit **event code** (derived from the canonical FQN)
* A canonical **event FQN string** label for logging/telemetry
* A map of diagnostic attributes (“context fields”)
* A list of **captured context frames**

The ABI defines only the **stable public layout**. Internal payload structures remain opaque.

### 2.1 C ABI representation

```c
typedef uint64_t DriftErrorCode;

typedef struct DriftError DriftError;

struct DriftError {
    DriftErrorCode code;      // Exception event code (see next section)
    DriftString    event_fqn; // Canonical FQN label ("module.sub:Event"), for logging only

    // Attribute list (key -> DiagnosticValue). The element layout is runtime-defined,
    // but the pointer+count positions are ABI-stable.
    void  *attrs;
    size_t attr_count;

    // Captured context frames list. The element layout is runtime-defined, but the
    // pointer+count positions are ABI-stable.
    void  *frames;
    size_t frame_count;
};
```

### 2.2 Guarantees

* `sizeof(DriftErrorCode) == 8`
* `DriftError.code` uses the ABI-stable event-code format described below; `0` is reserved for “unknown/unmapped”.
* The pointer+count fields have ABI-stable *positions*, but the contents behind them are **not ABI-stable** and are opaque to external callers.
* `event_fqn` stores the canonical FQN string; routing/matching is always by `code`, never by string compare.
* `Error` is always represented as a pointer handle (`DriftError*`) in the v1 runtime ABI (both intra-module and at module boundaries).

---

# 3. Exception Event Code ABI

*(Merged content from drift-abi-exceptions.md)*

Every error carries a **64-bit event code**:

```
bits 63..60 : domain tag (0b0001 for exceptions)
bits 59..0  : payload (60-bit hash)
```

User-defined exception events use:

```text
event_code = (0b0001 << 60) | (xxHash64(fqn_utf8) & ((1 << 60) - 1))
```

Where `fqn` is the canonical fully-qualified name `"module.sub:Event"` (dot-separated module path, colon before the event name, UTF-8 encoding, no aliases). No other domains (builtin/test) are defined in lang2 v1.

`event_code == 0` is reserved as an **“unknown/unmapped” sentinel** (e.g., missing catalog entry) and must not be produced by declared events. The encoded domain+hash form above always sets the high tag (`0b0001`), so valid events cannot collide with the reserved zero value.

---

# 4. Result<T, Error> and FnResult<T, Error> ABI

### 4.1 Conceptual model

Drift models fallible returns as:

```drift
variant Result<T, Error> {
    Ok(value: T)
    Err(error: Error)
}

alias FnResult<T, Error> = Result<T, Error>
```

Every Drift function that “can throw” is semantically returning `FnResult<T, Error>` **internally**.

At module boundaries, the ABI becomes a stable C layout.

---

## 4.2 Internal (intra-module) representation

Within a module, the compiler may use any efficient layout, as long as all call sites in the module agree.

The canonical v1 layout is:

```c
typedef struct {
    uint8_t    is_err;   // 0 = Ok, 1 = Err
    // padding as needed
    T          ok;       // Only valid when is_err = 0
    DriftError *err;     // Only valid when is_err = 1
} DriftFnResult_T_Error;
```

LLVM IR example for T = Int:

```llvm
%FnResult_Int_Error = type { i1, i64, %DriftError* }
```

This matches your MIR ops:

* `ConstructResultOk(dest, value)`
* `ConstructResultErr(dest, error)`
* And stage4’s typed FnResult-part checking.

---

## 4.3 Exported function ABI (module boundaries)

Any Drift function visible outside a compilation unit **must** use the exported ABI.

For:

```drift
fn f(x: Int) returns T
```

the exported ABI is always:

```
Result<T, Error>
```

That is, a struct:

### When `T` is sized (e.g., Int, Bool, Float):

```c
typedef struct {
    T           value;
    DriftError *error;   // NULL if Ok
} DriftResult_T_Error;
```

LLVM IR:

```llvm
{ T, %DriftError* }
```

### When `T` is `Void`:

```c
typedef struct {
    DriftError *error;   // NULL if Ok
} DriftResult_Void_Error;
```

LLVM IR:

```llvm
%DriftError*   ; error-only convention
```

Notes:

* Functions that syntactically “look like they return T” actually return `Result<T, Error>` at the ABI boundary.
* Internal-only functions may elide the Error part if proven not to throw.
* External callers **must** check for `error != NULL`.

---

# 5. Calling convention summary (v1)

### 5.1 Drift → LLVM rules

* Natural-width numeric types → fixed-size LLVM ints/floats
* `Bool` → `i1` for registers, `i8` for aggregates
* `Error` → `%DriftError` struct
* `FnResult<T, Error>` (internal) → `%FnResult_T_Error` struct
* Exported functions → `{ T, %DriftError* }` or `%DriftError*` for Void

### 5.2 Drift → C rules

* Exported functions always appear as C functions returning one of:

  ```c
  DriftResult_Int_Error  f(...);
  DriftResult_Void_Error f(...);
  ```

### 5.3 Pointers, slices, and user-defined types

(Not yet ABI-frozen; to be extended in later revisions.)

Current implementations may lower them opaquely through:

* `(T*, Size)` pairs,
* fat-pointer layouts, or
* internal pointer types,

but these are **not** part of v1 ABI yet.

---

# 6. Name mangling (placeholder)

Drift function names + signatures must map to globally unique C/LLVM symbols.

This document does **not** freeze the final mangling scheme.
Requirements:

* Must be collision-free across modules.
* Module name must be encoded.
* Signature (arg + return types) must be encoded.
* Backward compatibility rules will be specified when stabilizing ABI v1.

---

# 7. Stability & versioning

ABI-breaking changes:

* Changing scalar widths
* Changing Error layout or field order
* Changing event-code encoding
* Changing exported function Result layout
* Changing exception ABI hashing scheme or builtin payloads

ABI-compatible changes:

* Adding builtin event codes
* Extending hidden `Error` payload structures
* Adding new internal calling conventions for non-exported functions
* Adding new scalar types (as long as existing ones are unchanged)

---

# 8. Appendix: Example C header (v1)

```c
#ifndef DRIFT_LANG_ABI_V1_H
#define DRIFT_LANG_ABI_V1_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

// Scalars
typedef int64_t   DriftInt;
typedef uint64_t  DriftUint;
typedef uint64_t  DriftSize;
typedef uint8_t   DriftBool;
typedef double    DriftFloat;

// Error
typedef uint64_t DriftErrorCode;

	typedef struct DriftError {
	    DriftErrorCode code;
	    DriftString    event_fqn;
	    void          *attrs;
	    DriftSize      attr_count;
	    void          *frames;
	    DriftSize      frame_count;
	} DriftError;

// Result<Int, Error> for exported functions
typedef struct {
    DriftInt     value;
    DriftError  *error;   // NULL if Ok
} DriftResult_Int_Error;

typedef struct {
    DriftError  *error;   // NULL if Ok
} DriftResult_Void_Error;

#ifdef __cplusplus
}
#endif

#endif /* DRIFT_LANG_ABI_V1_H */
```

---
