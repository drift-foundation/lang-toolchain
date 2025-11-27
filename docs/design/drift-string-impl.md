# Drift String Implementation Notes

This document captures the runtime/ABI shape of `String` for the codegen path. It is implementation-facing (not surface language semantics) so we can wire LLVM/C stubs and keep the layout stable for future maintainers.

## Goals
- Heap-allocated, UTF-8 encoded strings.
- Deterministic ownership: caller owns `String`, must free explicitly when required.
- Opaque at the surface/DMIR level; layout is for runtime/ABI use only.
- Minimal helper set to support console I/O and basic concatenation/len.

## Runtime layout (proposed)

```c
struct DriftString {
    int64_t len;      // number of bytes (UTF-8), not including terminator
    char* data;       // heap-allocated, UTF-8; not null-terminated unless helper guarantees
};
```

Notes:
- `len` is signed 64-bit for consistency with other sizes.
- `data` points to a heap buffer owned by the `DriftString`.
- Helpers that expose C strings will ensure a temporary null terminator if needed; internally we treat strings as (ptr, len).

## ABI helpers (C side)

Provide these functions in the runtime C shim:

```c
// Allocate a DriftString from a null-terminated UTF-8 C string (copies the data).
struct DriftString drift_string_from_cstr(const char* cstr);

// Allocate a DriftString from a byte slice (ptr,len); copies the data.
struct DriftString drift_string_from_bytes(const char* data, int64_t len);

// Concatenate two strings; returns a new owned DriftString.
struct DriftString drift_string_concat(struct DriftString a, struct DriftString b);

// Free the heap buffer of a DriftString; safe to call on zeroed structs.
void drift_string_free(struct DriftString s);

// Convert DriftString to a null-terminated C string for printing; returns owned char* the caller must free.
char* drift_string_to_cstr(struct DriftString s);
```

Ownership rules:
- Each `DriftString` owns its `data`; concat returns a freshly allocated string; callers free via `drift_string_free`.
- `drift_string_to_cstr` allocates a buffer with a trailing `\0`; caller frees with `free()`.

## Codegen mapping

- The `String` type in MIR/LLVM lowers to `struct DriftString*` or by-value `struct DriftString` (choose one and stay consistent; simplest: by-value for now).
- String literals: codegen emits a global constant UTF-8 byte array and constructs a `DriftString` with `len` and `data` pointing to that constant (no heap free required for literals).
- `+` on `String`: lower to `drift_string_concat`.
- `out.writeln(String)` / `out.write(String)`: lower to C stubs that accept `DriftString` and print (using `drift_string_to_cstr` internally).

## Future extensions (not required for initial bring-up)
- `to_string` helpers for primitives (Int64, Bool, etc.) returning `DriftString`.
- Slices/views into strings (avoid copies).
- Small-string optimization (SSO) if needed for performance; would require a layout revision—document and version before changing.

## DMIR/Spec impact
- `String` remains opaque in the language/DMIR spec; this document defines the runtime ABI only.
- No DMIR signing changes are required as long as `String` fields are not exposed in DMIR.
