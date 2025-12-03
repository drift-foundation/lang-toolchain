# Drift exception ABI

This document defines the stable, cross-version ABI for Drift exception event codes. It is the canonical catalog for exception kinds and builtin exception codes. Language-level error-handling semantics are described in the main `drift-lang-spec.md`; this file only covers the low-level representation.

---

## Event code layout

Every exception value in Drift carries a 64-bit *event code* in `Error.code` (type `I64` in the language, `uint64_t` in C).

The layout is:

```text
bits 63..60 : kind    (4 bits)
bits 59..0  : payload (60 bits)
```

Let:

- `KIND_MASK   = 0xF000000000000000`  (top 4 bits)
- `PAYLOAD_MASK = 0x0FFFFFFFFFFFFFFF` (low 60 bits)

Then:

```c
uint64_t drift_event_kind(uint64_t code)    { return (code >> 60) & 0xF; }
uint64_t drift_event_payload(uint64_t code) { return code & PAYLOAD_MASK; }
```

The following kinds are currently defined:

| Kind name | Value (decimal) | Value (hex) | Description                                      |
|----------|------------------|-------------|--------------------------------------------------|
| TEST     | 0                | 0x0         | Test / dummy exceptions, not part of the public ABI. |
| USER     | 1                | 0x1         | User-defined exceptions (`module:Name`), hashed by FQN. |
| BUILTIN  | 2                | 0x2         | Builtin exceptions defined by this ABI document. |

Values `3..15` are reserved for future use. Any change to the meaning of these values is an ABI-breaking change.

### Construction

Given a 4-bit `kind` and a 60-bit `payload`:

```c
uint64_t drift_event_code(uint64_t kind, uint64_t payload60) {
    return (kind << 60) | (payload60 & PAYLOAD_MASK);
}
```

---

## User exception codes (kind = USER)

User-defined exceptions are assigned event codes by the compiler based on a canonical *fully-qualified name* (FQN):

```text
<module-name>:<ExceptionName>
```

If the exception is defined in a compilation unit without a module name, the FQN is:

```text
:<ExceptionName>
```

For a given FQN:

1. Encode as UTF‑8 bytes.
2. Compute `hash64 = xxHash64(fqn_bytes, seed = 0)`.
3. Compute `payload60 = hash64 & PAYLOAD_MASK`.
4. Compute `event_code = drift_event_code(USER, payload60)`.

The compiler enforces:

- Per-module collisions: two different FQNs in the same module may not map to the same `payload60`.
- In future, the linker/packer will enforce cross-module collisions using the exported metadata.

### ABI considerations

- The xxHash64 implementation and seed are ABI-frozen. Any change to the algorithm or seed is ABI-breaking.
- The compiler must use the same xxHash64 implementation for all user exceptions.

---

## Test/dummy exception codes (kind = TEST)

The runtime provides a helper for tests and non-ABI dummy errors:

```c
// test helper: constructs an Error with code + single (key, payload) arg
Error* drift_error_new_dummy(uint64_t raw_payload60, DriftString key, DriftString payload);
```

Semantics:

- `raw_payload60` is treated as an *unstructured* value for testing.
- The runtime constructs:

  ```c
  uint64_t payload60 = raw_payload60 & PAYLOAD_MASK;
  uint64_t code = drift_event_code(TEST, payload60);
  ```

- The resulting `Error` carries:

  - `code` as above, with kind = TEST.
  - `payload` as the first payload string (the language-level `Error.payload`).

These errors are **not** part of any stable ABI; consumers must not rely on particular test codes across versions.

---

## Builtin exception catalog (kind = BUILTIN)

Builtin exceptions are part of the core runtime and have fixed, stable event codes. Unlike user exceptions, their payload60 values are **explicit constants**, not hashes of FQNs.

All builtin exceptions use:

- `kind = BUILTIN = 2`
- `base = 2 << 60 = 0x2000000000000000`
- `event_code = base | payload60`

The following builtins are currently defined.

### Summary table

| Name              | FQN                      | Kind    | payload60 (dec) | payload60 (hex) | Event code (hex)        | Meaning                                                              |
|-------------------|--------------------------|---------|------------------|------------------|-------------------------|----------------------------------------------------------------------|
| OutOfMemory       | `builtin:OutOfMemory`    | BUILTIN | 1                | 0x0000_0000_0001 | 0x2000_0000_0000_0001   | Allocation failed; the runtime could not obtain memory.             |
| NullRef           | `builtin:NullRef`        | BUILTIN | 2                | 0x0000_0000_0002 | 0x2000_0000_0000_0002   | Dereference of a null reference or pointer-equivalent value.        |
| IndexOutOfBounds  | `builtin:IndexOutOfBounds` | BUILTIN | 3              | 0x0000_0000_0003 | 0x2000_0000_0000_0003   | Array or slice index was outside of `[0, len)`.                      |
| DivideByZero      | `builtin:DivideByZero`   | BUILTIN | 4                | 0x0000_0000_0004 | 0x2000_0000_0000_0004   | Division or modulo by zero.                                         |
| UnreachableHit    | `builtin:UnreachableHit` | BUILTIN | 5                | 0x0000_0000_0005 | 0x2000_0000_0000_0005   | Execution reached a path marked as unreachable.                     |
| Panic             | `builtin:Panic`          | BUILTIN | 6                | 0x0000_0000_0006 | 0x2000_0000_0000_0006   | Generic runtime panic (invariant violation, failed assertion, etc). |

Notes:

- FQNs for builtins all live in the synthetic `builtin` module.
- The payload60 assignments start at `1` and are dense; do not renumber existing entries.
- Adding a new builtin requires choosing a new, previously unused payload60 value.

### C definitions

An implementation may define these in a shared header as:

```c
#define DRIFT_EVENT_KIND_TEST      0ull
#define DRIFT_EVENT_KIND_USER      1ull
#define DRIFT_EVENT_KIND_BUILTIN   2ull

#define DRIFT_EVENT_PAYLOAD_MASK   0x0FFFFFFFFFFFFFFFull
#define DRIFT_EVENT_KIND_SHIFT     60

#define DRIFT_EVENT_CODE(kind, payload60) \
    ((((uint64_t)(kind)) << DRIFT_EVENT_KIND_SHIFT) | ((payload60) & DRIFT_EVENT_PAYLOAD_MASK))

#define DRIFT_EVENT_BASE_BUILTIN   (DRIFT_EVENT_CODE(DRIFT_EVENT_KIND_BUILTIN, 0ull))

#define DRIFT_EVENT_OOM_CODE            DRIFT_EVENT_CODE(DRIFT_EVENT_KIND_BUILTIN, 1ull)
#define DRIFT_EVENT_NULLREF_CODE        DRIFT_EVENT_CODE(DRIFT_EVENT_KIND_BUILTIN, 2ull)
#define DRIFT_EVENT_INDEX_OOB_CODE      DRIFT_EVENT_CODE(DRIFT_EVENT_KIND_BUILTIN, 3ull)
#define DRIFT_EVENT_DIVIDE_BY_ZERO_CODE DRIFT_EVENT_CODE(DRIFT_EVENT_KIND_BUILTIN, 4ull)
#define DRIFT_EVENT_UNREACHABLE_CODE    DRIFT_EVENT_CODE(DRIFT_EVENT_KIND_BUILTIN, 5ull)
#define DRIFT_EVENT_PANIC_CODE          DRIFT_EVENT_CODE(DRIFT_EVENT_KIND_BUILTIN, 6ull)
```

The compiler should mirror these constants when lowering builtin throws, ensuring that builtin exceptions and user exceptions share the same event-code machinery.

---

## Metadata export

Each compiled module should export exception metadata that includes:

- FQN (`"<module>:<Name>"` or `":<Name>"` for module-less).
- Kind (TEST/USER/BUILTIN).
- Payload60.
- Full event code.

The linker/packer is responsible for enforcing that no two distinct (kind, FQN) pairs produce the same `(kind, payload60)` across the final program.

For builtin exceptions, the metadata must match the catalog in this document exactly; any discrepancy is an ABI violation.

---

## ABI stability

The following changes are considered ABI-breaking and must not occur without an ABI version bump:

- Changing the bit layout of event codes.
- Changing the meaning or numeric value of any defined `kind`.
- Changing the xxHash64 algorithm or seed used for user exceptions.
- Changing any builtin’s FQN, payload60, or event code.
- Reusing a payload60 value assigned to a builtin for a different builtin.

Adding new builtin exceptions with previously unused payload60 values is ABI-safe, provided the existing entries remain unchanged.
