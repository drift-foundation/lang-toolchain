# Drift String Implementation Notes (ABI 22)

This document describes the runtime/ABI shape of `String` for the
codegen path and C integrators.  It is implementation-facing (not
surface language semantics): source-visible `String` behavior —
immutable UTF-8-conventioned bytes, `Copy` as retain, drop as release,
value equality/ordering — is defined by the language spec and is
unchanged by the representation.

> The earlier unique-owned `{len, char* data}` model this page once
> described (direct `free()`, per-literal data pointers, header behind
> the data pointer) is RETIRED as of ABI 22.  See `doc/history.md`
> (0.33.88) for the migration record.

The authoritative, compilable statement of the C surface is
`lang/language_runtime/string_runtime.h`; this page is the narrative
companion.

## Runtime layout

```c
typedef struct DriftRcBytes {
    _Atomic uint64_t strong;   /* offset 0 — refcount */
    _Atomic uint64_t flags;    /* offset 8 — see below */
    /* unsigned char bytes[];     offset 16 (tail): EXACTLY len+1
                                  bytes; bytes[len] == 0 */
} DriftRcBytes;                /* 16 bytes, 8-aligned (static-asserted) */

typedef struct DriftString {
    drift_isize   len;         /* byte length, excludes the hidden NUL */
    DriftRcBytes *storage;     /* -> header at OFFSET 0; bytes at +16 */
} DriftString;                 /* two words, passed BY VALUE */
```

* The handle stays two words by value; every helper keeps its ABI-21
  signature shape.  The pointer's meaning changed (header, not bytes)
  — which is exactly why the link stamp (`__drift_rt_abi_version_22`)
  gates mixing.
* Every constructor reserves exactly `len + 1` tail bytes and writes a
  hidden trailing NUL — the zero-copy borrowed-C-string invariant that
  `std.ffi.with_cstr*` builds on.
* Storage is exact: no offset/slice views inside the handle.
  `substring` allocates; a retained zero-copy `StringView` is a
  possible FUTURE API, deliberately not part of this representation.

## Flags

| bit | name | semantics |
|---|---|---|
| 0 | `STATIC` | compiler rodata literal; retain/release no-op |
| 1 | `IMMORTAL` | runtime-owned never-freed block (empty singleton, `from_bool` constants); retain/release no-op.  Mutually exclusive with `STATIC` |
| 2 | `NUL_SCANNED` | interior-NUL cache valid (bit 3 meaningful) |
| 3 | `HAS_INTERIOR_NUL` | valid only under bit 2 |

The NUL cache is monotonic write-once: one relaxed
`atomic_fetch_or` from the unknown state; concurrent scanners race
benignly to identical bits.  Reserved bits, `STATIC|IMMORTAL`, and
`HAS_INTERIOR_NUL` without `NUL_SCANNED` are unconditional contract
failures in every build.

## Ownership protocol

* `drift_string_retain` / `drift_string_release`: relaxed increment /
  release decrement + acquire fence before free (carried verbatim from
  the prior ABI).  Refcount overflow fails closed at
  `DRIFT_RC_MAX_LIVE`; release underflow aborts.
* By-value externs follow Convention A (caller-transferred stake,
  `DRIFT_OWNED_STRING`) or Convention B (borrowed pass-through) —
  see the header's convention comment and
  `test_drift_owned_string_audit.py`.

## Canonical empty and the internal tombstone (C integrators only)

* Every source-level empty String resolves to ONE runtime-owned
  immortal singleton (`__drift_rt_string_empty`, hidden linkage):
  `len == 0`, non-null storage whose byte area is a single NUL —
  C-string conversion of `""` succeeds.  Pointer identity is never
  String semantics.
* The all-zero handle `{0, NULL}` is a compiler-internal DROP-ONLY
  tombstone (the ownership pipeline's zero-storage doctrine writes it
  into dead slots).  `release`/`free` accept it as a no-op; every
  VALUE observation — the accessors, eq/cmp/concat, C-string
  conversion, retain — fails closed with `drift_contract_fail`
  instead of silently reading as empty.  **This state is not
  reachable from Drift source**; it concerns only C code that
  fabricates or zeroes handles.  Never construct it as "an empty
  string" — use `drift_string_empty()`.

## Access rules (layout authority)

Only `string_runtime.{h,c}` and three compiler lowerings (literal
emitters, `StringByteAt`, the private `string_bytes_base` intrinsic —
plus their shared observation guard) may touch storage/header layout.
Everyone else, runtime C included, uses the accessors:

```c
static inline drift_isize          drift_string_len (DriftString s);
static inline const unsigned char *drift_string_data(DriftString s);
```

Both validate the handle (tombstone/malformed/illegal flags fail
closed).  Enforced by `lang/tests/driver/test_string_layout_audit.py`,
which also forbids duplicate `struct DriftString` definitions — the
defect class that once let a stale-shape consumer misread headers.

## C-string bridge

```c
drift_isize drift_string_interior_nul_index(const DriftString *s);
char *drift_string_to_owned_cstr(const DriftString *s, drift_isize *nul_index_out);
char *drift_string_to_owned_cstr_unchecked(const DriftString *s);
void drift_cstr_free(char *p);
DriftCBytes drift_string_to_owned_cbytes(const DriftString *s);
void drift_cbytes_free(DriftCBytes b);
```

Allocator pairing is a CONTRACT, not a coincidence: allocations from
`drift_string_to_owned_cstr(_unchecked)` are freed ONLY with
`drift_cstr_free`, and `drift_string_to_owned_cbytes` blocks ONLY with
`drift_cbytes_free` — never a direct `free()`, even where the current
implementation would tolerate it.  `drift_string_to_cstr` (by-value,
allocating, owned by the caller) retains its historical name and
semantics.

## Literals

Compiler literals are emitted as `{ i64 1, i64 flags, [N+1 x i8] }`
rodata with the handle pointing at the HEADER (field 0); flags are
computed per literal at compile time (`STATIC | NUL_SCANNED`
[`| HAS_INTERIOR_NUL`]).  Empty literals lower to the runtime
singleton instead of per-module constants.

## Drift-side interop surface

User-facing borrowed/owned/scoped C interop lives in `std.ffi` — see
its module documentation and the Effective Drift "C interop for
`String`" section for usage guidance and examples.
