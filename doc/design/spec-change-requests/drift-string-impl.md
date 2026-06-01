# Drift String Implementation Notes

This document captures the runtime/ABI shape of `String` for the codegen path. It is implementation-facing (not surface language semantics) so we can wire LLVM/C stubs and keep the layout stable for future maintainers.

## Goals
- Heap-allocated, UTF-8 encoded strings.
- Deterministic ownership: caller owns `String`, must free explicitly when required.
- Opaque at the surface/DMIR level; layout is for runtime/ABI use only.
- Lengths and indices use the language-level `Size` type rather than a hard-coded integer width.
- Minimal helper set to support console I/O and basic concatenation/len.

## Runtime layout (proposed)

```c
// `drift_size_t` is the C carrier for Drift's `Size` type.
// It must match the ABI representation of `Size` on the current target.
typedef size_t drift_size_t;

struct DriftString {
    drift_size_t len;  // number of bytes (UTF-8), not including terminator; Drift `Size`
    char* data;        // heap-allocated, UTF-8; helpers keep a trailing NUL in data
};
```

Notes:
- `len` is conceptually a `Size` in the Drift spec. At the ABI level it is represented by `drift_size_t`, which must be kept in sync with the ABI carrier for `Size`.
- `data` points to a heap buffer owned by the `DriftString`.
- Helpers that expose C strings will ensure a temporary null terminator if needed; internally we treat strings as `(ptr, len)`.

## ABI helpers (C side)

Provide these functions in the runtime C shim:

```c
// Allocate a DriftString from a null-terminated UTF-8 C string (copies the data).
struct DriftString drift_string_from_cstr(const char* cstr);

// Allocate a DriftString from a byte slice (ptr,len); copies the data and appends a NUL.
struct DriftString drift_string_from_utf8_bytes(const char* data, drift_size_t len);

// Concatenate two strings; -> a new owned DriftString.
struct DriftString drift_string_concat(struct DriftString a, struct DriftString b);

// Free the heap buffer of a DriftString; safe to call on zeroed structs.
void drift_string_free(struct DriftString s);

// Convert DriftString to a null-terminated C string for printing; -> owned char* the caller must free.
char* drift_string_to_cstr(struct DriftString s);
```

Ownership rules:
- Each `DriftString` owns its `data`; concat returns a freshly allocated string; callers free via `drift_string_free`.
- `drift_string_to_cstr` allocates a buffer with a trailing `\0`; caller frees with `free()`.

## Codegen mapping

- The `String` type in DMIR/LLVM lowers to `struct DriftString` by value (or a stable pointer to it; choose one and stay consistent).
- In the surface language, `String.len()` (or equivalent) returns `Size`, matching the `len` field's logical type.
- String literals: codegen emits a global constant UTF-8 byte array and constructs a `DriftString` with `len: Size` and `data` pointing to that constant (no heap free required for literals).
- `+` on `String`: lower to `drift_string_concat`.
- `out.writeln(String)` / `out.write(String)`: lower to C stubs that accept `DriftString` and print (using `drift_string_to_cstr` internally).

## Future extensions (not required for initial bring-up)
- `to_string` helpers for primitives (Int, Bool, etc.) returning `DriftString`.
- Slices/views into strings (avoid copies).
- Small-string optimization (SSO) if needed for performance; would require a layout revision—document and version before changing.

## DMIR/Spec impact
- `String` remains opaque in the language/DMIR spec; this document defines the runtime ABI only.
- All public length/offset APIs on `String` must use `Size`, not `Int64`, to stay consistent with the core spec.
- No DMIR signing changes are required as long as `String` fields are not exposed in DMIR.
