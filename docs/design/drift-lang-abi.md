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

* **Natural width primitives:** pointer-sized (isize/usize) for target-native ABI
* **Fixed-width primitives:** identical to C/LLVM integer/float widths

### 1.1 Natural-width primitives

| Drift type | C ABI type           | LLVM IR                           | Notes                                  |
| ---------- | -------------------- | --------------------------------- | -------------------------------------- |
| `Int`      | `ptrdiff_t`          | `%drift.isize`                    | Signed, pointer-sized.                 |
| `Uint`     | `size_t`             | `%drift.usize`                    | Unsigned, pointer-sized.               |
| `Size`     | (reserved)           | (reserved)                        | Not used in v1.                        |
| `Bool`     | `uint8_t`            | `i1` (in regs), `i8` (in structs) | ABI defines on-wire as 1 byte.         |
| `Float`    | `double`             | `double`                          | IEEE-754 64-bit float.                 |

### 1.2 Fixed-width primitives

Fixed-width primitives are ABI-defined but **reserved in v1 surface code**; they are permitted only in `lang.abi.*` and internal runtime/compiler types. `Byte` remains a v1 surface scalar (maps to `uint8_t`/`i8`) even though `Uint8` is reserved.

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
* A **`params` JSON document** holding the declared exception field values
* A **`context` JSON document** holding `^`-captured frames as a JSON array
* An opaque captured backtrace handle

The ABI defines only the **stable public layout**. Internal payload structures remain opaque.

> **Migration state (Slice 7c-1, 0.31.64, 2026-05-06).** Current runtime is **ABI 14**.  The public DV surface is removed (Slice 7a) and the runtime DV exports are deleted (Slice 7c-1).  Throw-side params projection is owned by `core.Diagnostic for E`'s `to_json_text(&E)` impl; captured-locals frame JSON is built by direct per-scalar dispatch to the `core.diagnostic_json_*` helpers.  No DV-attachment, no DV intermediate.
>
> **What remains as compiler-internal carrying cost at ABI 14** (slated for Slice 7c-2): the HIR `H.HDVInit` node + `_visit_expr_HDVInit` lowering; the MIR op classes `M.ConstructDV`, `M.ErrorAddAttrDV`, `M.ErrorAddLocalDV`, `M.ErrorAttrsGetDV`, `M.ErrorCapturesGetDV`, `M.DVAs*`, `M.DVKind`, `M.DVIndex`, `M.DVLen`, `M.DVEntries`, `M.DVGetField`; the compiler-intrinsic `DiagnosticValue` TypeKind; and a `DriftDiagnosticValue` LLVM type alias and C struct definitions.  No production lowering reaches any of these — codegen handlers ICE on contact.  Slice 7c-2 deletes the type-table / package-serialization / HIR / MIR substrate.
>
> **ABI version trajectory:**
>
> * **ABI 11** — Phase 0 spec / Phase 1 prep substrate (runtime helpers staged but not yet emitted by the compiler).
> * **ABI 12** — Slices 1 / 2 / 3 (additive).  Compiler emits calls to `drift_dv_kind`, `drift_dv_index`, `drift_error_get_params_json`, `drift_error_set_params_json`, `drift_error_get_context_json`, `drift_error_append_context_frame`, and (for Slice 3 envelope assembly) `drift_string_retain`.  Both the legacy DV path AND the new JSON-text path are operational; `<error>.params.encode_compact()`, `<error>.context.encode_compact()`, and `e.encode_compact()` return canonical JSON documents.
> * **ABI 13** — Slice 7a (0.31.62) cuts the public DV/DE surface and migrates `Diagnostic.to_diag(self) -> DV` → `to_json_text(self) -> String`.  Slice 7b (0.31.63) retires the DV-attachment lowering: throw-side params and captured-locals frame JSON both flow through `core.Diagnostic.to_json_text` / `core.diagnostic_json_*` directly.  Runtime DV helpers stay exported in this slice for binary compatibility with ABI 13 consumers; new compilations no longer call them.
> * **ABI 14** — Slice 7c-1 (current; 0.31.64).  Runtime DV exports deleted: `drift_dv_*`, `drift_error_add_attr_dv`, `drift_error_add_local_dv`, `__exc_attrs_get_dv`, `__exc_captures_get_dv`, `drift_error_new_with_payload`, `drift_diag_from_int`, `drift_diag_from_string` are gone from the runtime archive.  The on-runtime `DriftError` struct's `attrs` / `attr_count` / `frames` / `frame_count` fields are deleted along with the matching `DriftErrorAttr` / `DriftErrorLocal` / `DriftCtxFrame` storage types.  Breaking — old (ABI 13) binaries that still call the deleted helpers fail to link against the ABI 14 runtime.  No compatibility shim; the link-time `__drift_rt_abi_version_14` stamp is the contract gate.
>
> Consumers compiled against any ABI ≤ 13 must be rebuilt against ABI 14.  ABI 14's break is enforced by the link-time ABI stamp.

### 2.1 C ABI representation

```c
typedef uint64_t DriftErrorCode;

typedef struct DriftError DriftError;

struct DriftError {
    DriftErrorCode code;          // Exception event code (see §3)
    DriftString    event_fqn;     // Canonical FQN label ("module.sub:Event"), logging only

    // Declared exception fields, serialized as a JSON object.
    // Each declared field is stored under its source-level name; the value is the
    // result of Diagnostic.to_json() (lang-spec §5.13.7) re-encoded as JSON text.
    // Storage is owned by the runtime; the pointer is valid for the lifetime of
    // the DriftError. The textual encoding is UTF-8, RFC-8259 conformant.
    DriftString    params_json;

    // ^-captured frames, serialized as ONE canonical JSON array document
    // (a single DriftString, not an array-of-strings):
    //     [ { "fn_name": "...", "locals": { "<name>": <JsonNode>, ... } }, ... ]
    // Frames appear in unwind order (innermost first). A function that unwinds
    // without any ^-capture contributes no entry.  Empty form is the two-byte
    // literal "[]".
    DriftString    context_json;

    // Opaque captured backtrace.  Layout runtime-defined; pointer position is
    // ABI-stable.
    void          *stack;
};
```

### 2.2 Guarantees

* `sizeof(DriftErrorCode) == 8`
* `DriftError.code` uses the ABI-stable event-code format described in §3; `0` is reserved for “unknown/unmapped”.
* `event_fqn` stores the canonical FQN string; routing/matching is always by `code`, never by string compare.
* `params_json` and `context_json` are well-formed JSON in UTF-8.  An empty error has `params_json == "{}"` and `context_json == "[]"`.
* The textual encoding (whitespace, key ordering) inside `params_json` / `context_json` is **not** ABI-stable; consumers must parse, never byte-compare.
* **Dump fast path.** Lang-level `e.encode_compact()` (lang-spec §14.5.4 / §14.8) is permitted — and expected — to splice `params_json` and `context_json` directly into the envelope without parse/materialize/re-encode. Helpers MUST therefore preserve the exact byte sequence of `params_json` / `context_json` from the most recent setter call until they are next replaced.
* `stack` is opaque; callers obtain a printable form through the runtime backtrace API, not by dereferencing the field.
* `Error` is always represented as a pointer handle (`DriftError*`) in the v1 runtime ABI (both intra-module and at module boundaries).

### 2.3 Runtime helpers

Helper signatures the runtime exposes for building and reading errors. Each helper specifies its ownership contract; any deviation is a runtime bug:

```c
// Construct a fresh error with empty params/context.  The runtime
// makes its own owned copy of `event_fqn` (via the string-runtime's
// header-allocating copy path); the caller retains ownership of the
// input.  This is intentionally robust to all caller patterns —
// heap-allocated DriftStrings, LLVM-emitted static-flagged literals,
// and raw cstring `{len, ptr}` shapes constructed by C runtime
// helpers all work uniformly without the caller knowing the
// allocation class.
DriftError *drift_error_new(DriftErrorCode code, DriftString event_fqn);

// Replace params_json on an in-flight error. Takes ownership of params_json.
// Caller must not retain or read the prior params_json after this call.
void drift_error_set_params_json(DriftError *err, DriftString params_json);

// Append one frame object (already serialized as JSON) to context_json.
// Takes ownership of frame_json. The runtime re-emits context_json with the
// new frame appended; the caller does not own the merged buffer.
void drift_error_append_context_frame(DriftError *err, DriftString frame_json);

// Read params_json / context_json. The returned DriftString is RETAINED
// (refcount bumped); the caller owns the returned reference and is responsible
// for releasing it.  Safe to surface to lang code as a normal owned `String`
// return without compiler-side borrow handling.
DriftString drift_error_get_params_json(const DriftError *err);
DriftString drift_error_get_context_json(const DriftError *err);
```

The earlier `drift_error_add_attr_dv`, `drift_error_add_local_dv`, `__exc_attrs_get_dv`, `__exc_captures_get_dv`, `drift_error_new_with_payload`, and the `drift_dv_*` family (constructors, accessors, conversions, clone, release, kind, index, len, entries, get_field) are deleted at ABI 14 / Slice 7c-1.  ABI 13 binaries that still reference any of them fail to link against the ABI 14 runtime archive.

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

Where `fqn` is the canonical fully-qualified name `"module.sub:Event"` (dot-separated module path, colon before the event name, UTF-8 encoding, no aliases). No other domains (builtin/test) are defined in lang v1.

`event_code == 0` is reserved as an **“unknown/unmapped” sentinel** (e.g., missing catalog entry) and must not be produced by declared events. The encoded domain+hash form above always sets the high tag (`0b0001`), so valid events cannot collide with the reserved zero value.

---

# 4. Interface Values and Vtables (v1)

## 4.1 Interface value representation

All interface values are fat pointers with inline storage:

```
%DriftIface = { i8* data_ptr, i8* vtable_ptr, [INLINE_WORDS x usize] inline_payload, i8 is_inline }
```

`INLINE_WORDS = 4`, so `INLINE_BYTES = pointer_width * 4`.

* `data_ptr` points to the boxed concrete value (owned by the interface value) when `is_inline == 0`.
* `vtable_ptr` points to the **start of the segment** for the *static interface type* of the value.
* When `is_inline == 1`, the concrete value is stored in `inline_payload` and `data_ptr` is ignored.

Throwing callback interfaces (`CallbackThrowN`) use the same interface value layout as
`CallbackN`. Their `call` slot follows the throwing ABI (returns a `FnResult`-shaped
value), but the data/vtable representation is unchanged.

Captured environments for `CallbackN`/`CallbackThrowN` are stored in the interface
value’s data payload (boxed or inline); captures are not treated specially by the ABI.

## 4.2 Vtable layout: segment-per-interface

Each interface `I` has a **segment**:

```
Segment(I) = [ drop_ptr, I.method0, I.method1, ... ]
```

* **Slot 0** is the drop function pointer (`void (i8*)*`) for the boxed value.
* Method slots follow in **declared method order**.

A vtable object for a boxed value of static interface type `C` is the **concatenation** of segments for `C` and its ancestors:

```
VTable(C) = Segment(C) | Segment(parent1) | Segment(parent2) | ...
```

The concatenation order is deterministic (see §4.4).

## 4.3 Method dispatch

* The call site computes the slot index for the **static interface type**:

```
slot = offset(segment_of_owner_interface) + 1 + method_index
```

* `vtable_ptr` points at the start of the segment for the static interface type, so `slot=1` is the first method of that interface.

## 4.4 Interface inheritance linearization

For an interface `C` that extends parents `P1, P2, ...`:

* Build the ancestor DAG with edges `Child -> Parent`.
* Compute a **deterministic topological order** of the ancestor set:
  1. Primary tie-break: **declared parent order** at the closest common child.
  2. Secondary tie-break: **fully-qualified interface name** lexical order.
* `linearization(C) = [C] + ordered_unique_ancestors`.

Diamonds are **deduped**: each ancestor appears at most once.

## 4.5 Upcast rule

Upcasting `C -> A` **retargets** the vtable pointer:

```
vtable_ptr = vtable_ptr + offset(Segment(A) in VTable(C))
```

`data_ptr` is unchanged.

## 4.6 Ownership and drop

Interface values **own** their boxed data. Dropping an interface value:

* loads `drop_ptr` from slot 0 of the current segment,
* calls `drop_ptr(data_ptr)` if non-null.

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
    T          ok;       // Only valid when is_err = 0 (stored in aggregate form)
    DriftError *err;     // Only valid when is_err = 1
} DriftFnResult_T_Error;
```

LLVM IR example for T = Int:

```llvm
%FnResult_Int_Error = type { i1, %drift.isize, %DriftError* }
```

Notes:

* `T` in `FnResult<T, Error>` uses its **aggregate/storage** representation.
  For example, `Bool` is stored as `i8` (with `icmp ne` on load and `zext` on store).
* `Int`/`Uint` are pointer-sized in v1 (`isize`/`usize`), and `Float` is target-specific
  (typically `double`/8 bytes).

This matches your MIR ops:

* `ConstructResultOk(dest, value)`
* `ConstructResultErr(dest, error)`
* And stage4’s typed FnResult-part checking.

---

## 4.3 Exported function ABI (module boundaries)

Any Drift function visible outside a compilation unit **must** use the exported ABI.

For:

```drift
fn f(x: Int) -> T
```

the exported ABI is always:

```
Result<T, Error>
```

That is, a struct:

### When `T` is sized (e.g., Int, Bool, Float):

---

# 5. Variant ABI (intra-module, compiler-private)

This section documents the **current compiler layout** for general `variant` values.
It is **not** part of the stable exported ABI. It only needs to be self-consistent
within a module so that all call sites agree.

The canonical v1 layout used by LLVM codegen is:

```c
typedef struct {
    uint8_t tag;       // constructor tag (0..N-1)
    // padding to payload alignment
    payload_cell_t payload[payload_words];
} DriftVariant;
```

Where:
- `payload_cell_t` is an integer cell sized to the payload alignment (e.g., `i64` on 64-bit targets).
- `payload_words` is the number of cells required to fit the largest constructor payload.
- Payload alignment is the maximum alignment across constructor fields (at least word size).

Per-constructor payloads are packed as a literal struct of field storage types:
- `Bool` is stored as `i8` inside payloads (aggregate/storage form).
- Other fields use their aggregate/storage representation.

Notes:
- This layout is used for all variants (including `Optional<T>` and `Result<T, Error>`) **inside a module**.
- The exported ABI for `Result<T, Error>` remains the stable layout described in §4.3.

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

Notes:

* `T` uses its **aggregate/storage** representation at the boundary. `Bool` is
  stored as `i8` (wrappers `zext i1 -> i8` when returning `Bool`).
* `Int`/`Uint` are pointer-sized in v1 (`isize`/`usize`), and `Float` is target-specific
  (typically `double`/8 bytes).

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

* `(T*, Uint)` pairs,
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

## 7.1 ABI version stamping (link-time guard)

The compiler and runtime use a link-time ABI compatibility guard to fail deterministically at build time when their ABI expectations disagree.

### Mechanism

1. **Single source of truth:** `lang/driftc/driftc_versions.py` defines `DRIFT_RT_ABI_VERSION` (monotonic integer).
2. **Runtime side:** `lang/language_runtime/abi_version_stamp.c` exports a strong symbol `__drift_rt_abi_version_<N>` (compiled into all runtime archive variants via `-DDRIFT_RT_ABI_VERSION=N`).
3. **Compiler side:** The codegen entry wrapper (`@main`) emits `call void @__drift_rt_abi_version_<N>()` using the same constant.
4. **Link contract:** If compiler and runtime agree on `<N>`, linking succeeds. On mismatch the linker reports an unresolved symbol — fast, deterministic, no runtime crash.

### When to bump

Bump `DRIFT_RT_ABI_VERSION` when changing any compiler/runtime boundary contract:

* Runtime-exported helper signatures consumed by codegen
* Data layouts crossing the boundary (struct/variant/frame payload ABI)
* Calling convention or return packing between generated code and runtime
* Ownership/drop contract changes that alter boundary behavior

Do **not** bump for pure internal refactors with no boundary change.

### Driver diagnostics

When the `driftc` driver detects `__drift_rt_abi_version_` in linker error output, it appends a hint:

```
hint: driftc targets runtime ABI vN; linked runtime provides a different ABI.
      Rebuild runtime/std artifacts (just runtime-libs).
```

### Contributor guidance

* After making an ABI-breaking change, bump the integer in `lang/driftc/driftc_versions.py`.
* Rebuild all runtime archive variants (`just runtime-libs`).
* Add or update positive and negative regression tests in `lang/tests/driver/test_abi_version_stamp.py`.

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

// Scalars (pointer-sized Int/Uint)
typedef ptrdiff_t DriftInt;
typedef size_t    DriftUint;
typedef uint8_t   DriftBool;
typedef double    DriftFloat;

// Error
typedef uint64_t DriftErrorCode;

	typedef struct DriftError {
	    DriftErrorCode code;
	    DriftString    event_fqn;
	    DriftString    params_json;     // JSON object of declared exception fields
	    DriftString    context_json;    // JSON array of ^-captured frame objects
	    void          *stack;           // opaque backtrace
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
