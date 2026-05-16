# Slice: `DRIFT_OWNED_STRING` runtime hygiene

**Goal:** make the Drift→C "by-value `DriftString` = callee releases" ABI
contract impossible to forget WITHIN annotated functions, and
lint-enforced for every receiver that crosses the boundary.

**Scope:** runtime only (`lang/language_runtime/`).  No compiler changes,
no Drift-side changes, no ABI header changes.

**Status:** queued; do not start until full e2e is green on this branch.

---

## Background

The Drift→C ABI for `DriftString` by-value parameters is "callee releases
the transferred refcount stake exactly once before returning."  The
caller's IR retains the string immediately before the extern call, so
the C function owns that stake on entry.  Forgetting to release at the
callee leaks one refcount per call.

Recent failures:
- `drift_io_open` (0.31.91 family) — every `io.file_builder(...).build()`
  leaked the path String.
- `drift_env_get` / `drift_env_has` (same slice) — same shape, masked in
  fixtures by static-flagged string literal args.
- `drift_assert_loc` — happy-path early return skips release on
  `file`/`expr`/`msg` (open audit note, low priority).
- `drift_bounds_check` — happy-path early return skips release on
  `container_id` (open audit note, low priority).

The convention is implicit; reviewer memory is the only safeguard.  This
slice replaces convention with `__attribute__((cleanup(...)))` for
automatic release at scope exit AND a static lint that requires every
by-value receiver to either use the macro or carry an explicit allow
marker.

---

## Deliverables

1. `DRIFT_OWNED_STRING` macro in `string_runtime.h`.
2. Convert all by-value `DriftString` receivers in
   `console_runtime.c`, `env_runtime.c`, `posix/io_runtime.c`,
   `posix/assert_runtime.c`, `array_runtime.c` to the macro form, with
   careful handling for delegating wrappers (see "Console family"
   below).
3. Static audit test `lang/tests/lang_runtime/test_drift_owned_string_audit.py`
   that scans .c/.h files under `lang/language_runtime/` and requires
   every by-value `DriftString` receiver to use the macro OR carry an
   explicit allow marker.
4. `docs/history.md` entry; `lang/versions.py` minor bump.

---

## Macro placement

`lang/language_runtime/string_runtime.h`, immediately after
`drift_string_release`:

```c
/* By-value DriftString ABI contract:
 * The Drift caller transfers a refcount stake to the C callee.
 * The callee MUST release that stake exactly once before returning.
 * Annotate received-by-value parameters with DRIFT_OWNED_STRING
 * (using a local copy) to make the release automatic at every scope
 * exit -- no per-return-path drift_string_release() calls needed.
 *
 * Adoption is enforced by
 * lang/tests/lang_runtime/test_drift_owned_string_audit.py, which
 * fails CI if a by-value DriftString receiver lacks either the macro
 * or an explicit allow marker. */
static inline void _drift_string_cleanup(DriftString *s) {
    drift_string_release(*s);
}
#define DRIFT_OWNED_STRING __attribute__((cleanup(_drift_string_cleanup)))
```

Headers stay unchanged — parameters remain `DriftString` at the ABI
boundary.  The `_in → DRIFT_OWNED_STRING local` copy is the call-site
boilerplate.

---

## Conversions

### Standard shape (single by-value param, no delegation)

`posix/io_runtime.c::drift_io_open`:

```c
int64_t drift_io_open(DriftString path_in, int64_t flags, int64_t mode) {
    DRIFT_OWNED_STRING DriftString path = path_in;
    char *cstr = drift_string_to_cstr(path);
    int fd = open(cstr, (int)flags, (mode_t)mode);
    int err = errno;
    free(cstr);
    errno = err;
    return (int64_t)fd;
}
```

`env_runtime.c::drift_env_get`, `drift_env_has`: same pattern, drop the
explicit `drift_string_release(name)` line, add the cleanup attribute on
a local copy.

### Multi-param shape

`posix/assert_runtime.c::drift_assert_loc(int cond, DriftString file,
drift_isize line, DriftString expr, DriftString msg)`:

```c
void drift_assert_loc(int cond,
                      DriftString file_in, drift_isize line,
                      DriftString expr_in, DriftString msg_in) {
    DRIFT_OWNED_STRING DriftString file = file_in;
    DRIFT_OWNED_STRING DriftString expr = expr_in;
    DRIFT_OWNED_STRING DriftString msg  = msg_in;
    if (cond) {
        return;  /* cleanup fires here -- closes the happy-path leak */
    }
    /* ... existing failure-path code unchanged ... abort() at the end. */
}
```

Closes the audit note: the previously-leaking happy-path return now
automatically releases all three strings.  The failure path ends in
`abort()`; `cleanup` does not fire on abort, but the process is dying
so the leak is moot — keep `abort()` unchanged.

`array_runtime.c::drift_bounds_check(struct DriftString container_id,
drift_isize idx, drift_isize len)`:

```c
void drift_bounds_check(struct DriftString container_id_in,
                        drift_isize idx, drift_isize len) {
    DRIFT_OWNED_STRING DriftString container_id = container_id_in;
    if (idx < 0 || idx >= len) {
        drift_bounds_check_fail(container_id, idx, len);
        /* unreachable: drift_bounds_check_fail is noreturn */
    }
    /* happy path falls through to scope exit -> cleanup fires. */
}
```

Note: `drift_bounds_check_fail` is `__attribute__((noreturn))`, so the
cleanup attribute does NOT fire on the failure path (control never
returns to drift_bounds_check's frame).  This matches the current
behavior — the failure path's `container_id` is consumed by
`drift_bounds_check_fail` which never returns; the process dies via the
diagnostic-throw machinery downstream.

### Console family — CAREFUL (delegation hazard)

Today's shape:

```c
void drift_console_write(DriftString s) {
    /* ... fwrite ... */
    drift_string_release(s);
}

void drift_console_writeln(DriftString s) {
    drift_console_write(s);   /* delegates; releases inside */
    fputc('\n', stdout);
}
```

If we naively annotate both with `DRIFT_OWNED_STRING`, the outer
`writeln` releases via cleanup AND the inner `write` releases via its
own cleanup → **double release on the same stake → abort or UAF**.

**Resolution: extract a borrowed-input helper.**

```c
/* Internal: writes s to stdout without taking ownership.
 * Caller retains the refcount stake. */
static void _drift_console_write_borrowed(DriftString s) {
    if (s.len == 0 || s.data == NULL) {
        return;
    }
    size_t n = (size_t)s.len;
    size_t written = fwrite(s.data, 1, n, stdout);
    if (written < n && ferror(stdout)) {
        abort();
    }
}

void drift_console_write(DriftString s_in) {
    DRIFT_OWNED_STRING DriftString s = s_in;
    _drift_console_write_borrowed(s);
}

void drift_console_writeln(DriftString s_in) {
    DRIFT_OWNED_STRING DriftString s = s_in;
    _drift_console_write_borrowed(s);
    fputc('\n', stdout);
}
```

Same shape for `_drift_console_eprint_borrowed` + `drift_console_eprint`
+ `drift_console_eprintln`.

The borrowed helpers are `static` (file-local, no ABI exposure) and
take `DriftString` by value at the C level, but the caller's stake is
NOT transferred — they're pure read borrows.  Audit must allow these.

### Sites NOT to convert (by design)

`string_runtime.c` exports the refcount primitives themselves:

| Function | Why excluded |
|---|---|
| `drift_string_retain(s)` | returns retained s; deliberately keeps stake |
| `drift_string_release(s)` | this IS the release primitive |
| `drift_string_free(s)` | thin alias for release (same semantics) |
| `drift_string_concat(a, b)` | consumes a + b explicitly via internal alloc + manual release; the release IS in the body |
| `drift_string_eq(a, b)`, `drift_string_cmp(a, b)` | pure read; do NOT release — refcount stays with caller |
| `drift_string_to_cstr(s)` | pure read; does NOT consume s; returns new cstring |

These need explicit allow markers (see below) because they take
`DriftString` by value but do not follow the "callee releases" rule.

`array_runtime.c::drift_bounds_check_fail` — `noreturn`, ends in
process death via the diagnostic-throw path; release-vs-not is moot.
Allow marker.

---

## Static audit

New test `lang/tests/lang_runtime/test_drift_owned_string_audit.py`.

**Scope:** `.c` files under `lang/language_runtime/` (not headers — the
declarations don't have bodies to enforce against).

**Algorithm:**
1. For each `.c` file, scan for function definitions whose signature
   includes one or more `DriftString <name>` (by-value) parameters.
   Excludes `DriftString *<name>` (pointer / borrow form).
2. For each such function, the body must satisfy ONE of:
   - **(a)** Every by-value `DriftString` parameter is shadowed by a
     `DRIFT_OWNED_STRING DriftString <local> = <param>;` within the
     first ~10 lines of the body.  (Allows `param_in → local` pattern
     OR direct `DRIFT_OWNED_STRING DriftString s = s;` shadow.)
   - **(b)** The function carries an explicit allow marker (see below)
     within the first ~10 lines, naming each unannotated parameter.

**Allow marker format:**

```c
/* drift-owned-string-audit: allow <reason> -- <param-names> */
```

Examples:

```c
/* drift-owned-string-audit: allow refcount-primitive -- s */
DriftString drift_string_retain(DriftString s) { ... }

/* drift-owned-string-audit: allow read-only-borrow -- a, b */
int drift_string_eq(DriftString a, DriftString b) { ... }

/* drift-owned-string-audit: allow consumed-by-noreturn-callee -- container_id */
void drift_bounds_check_fail(struct DriftString container_id, ...) { ... }

/* drift-owned-string-audit: allow internal-borrowed-helper -- s */
static void _drift_console_write_borrowed(DriftString s) { ... }
```

Allowed reason vocabulary (lint enforces — typos fail):
- `refcount-primitive` — function IS the retain/release/concat/free primitive
- `read-only-borrow` — pure read; refcount unchanged
- `consumed-by-noreturn-callee` — passed to a `noreturn` function that consumes it
- `internal-borrowed-helper` — static helper whose callers own the release
- `audit-pending` — temporary escape; lint emits warning (not error) for these; track in issue list

**Implementation:** regex-based scanner; ~120 LOC including the marker
parser and a small fixture suite that exercises positive
(annotated) and negative (unannotated, no marker) cases.

Hook into CI via `just test` (will auto-pick-up under the existing
`lang/tests/` discovery).

---

## Portability constraints

- `__attribute__((cleanup(fn)))` is a GCC extension supported by Clang.
  Drift's toolchain ships only those two (`lang/language_runtime/
  __init__.py::build_runtime_archive` invokes clang).  No MSVC concern.
- Drift already builds with `-std=c11` and accepts GNU extensions — no
  flag change required.
- Cleanup fires on every scope exit including `return`, fall-through,
  `goto`.  Does NOT fire on `longjmp` past the frame OR on `abort()` /
  other `noreturn` paths that don't unwind.  None of the runtime
  receivers use `longjmp` (the only `longjmp` is the fiber switch via
  ucontext, which isn't crossed by these calls), so the only "doesn't
  fire" case is the abort path — desirable since the process is dying.
- The `cleanup` attribute applies to the LOCAL, not the parameter.  The
  `param_in → local` copy is mandatory; you cannot annotate a parameter
  directly.  This is the one bit of boilerplate per converted function.

---

## Files touched

| File | Change |
|---|---|
| `lang/language_runtime/string_runtime.h` | add `_drift_string_cleanup` + `DRIFT_OWNED_STRING` macro after `drift_string_release` declaration |
| `lang/language_runtime/string_runtime.c` | add allow markers on `retain` / `release` / `free` / `concat` / `eq` / `cmp` / `to_cstr` |
| `lang/language_runtime/console_runtime.c` | extract `_drift_console_write_borrowed` and `_drift_console_eprint_borrowed`; rewrite the 4 public entry points to use the borrowed helper + `DRIFT_OWNED_STRING` |
| `lang/language_runtime/env_runtime.c` | convert `drift_env_get`, `drift_env_has` |
| `lang/language_runtime/posix/io_runtime.c` | convert `drift_io_open` (drop the explicit release line landed this slice's family) |
| `lang/language_runtime/posix/assert_runtime.c` | convert `drift_assert_loc` (3 params); closes the happy-path audit note |
| `lang/language_runtime/array_runtime.c` | convert `drift_bounds_check`; allow marker on `drift_bounds_check_fail` |
| `lang/tests/lang_runtime/test_drift_owned_string_audit.py` | new file, ~120 LOC |
| `docs/history.md` | new top entry |
| `lang/versions.py` | minor bump (`0.31.91 → 0.31.92`); ABI unchanged at 14 |

**Patch size estimate:**
- Macro + helper: ~10 LOC.
- Per-function conversion: ~3 LOC each × 8 converted functions = ~25 LOC.
- Console borrowed-helper extraction: net +15 LOC (2 helpers added, 4 entries simplified).
- Allow markers on string_runtime primitives + bounds_check_fail: ~8 LOC.
- Static audit test: ~120 LOC.
- **Total: ~180 LOC, of which ~120 is the test.  Production code delta is small.**

---

## Risk assessment

**Low risk.**

- Macro semantics are well-established compiler features; no novel C.
- Each converted function is mechanical: rename param → `_in`, add
  shadowing local with attribute, remove explicit release.  IR diff at
  the C-compile level should be empty modulo register allocator
  reshuffling (the release call moves from "before return" to "scope
  exit at compiler-injected destructor" — same semantics).
- Console family has the one trap (delegation double-release), handled
  by extracting borrowed helpers.
- Static audit is non-invasive; failures are pre-merge.

**Verification:**
- All existing memcheck-clean tests (`std_io_*`, `env_*`,
  `condvar_*`, ...) MUST remain memcheck-clean after the slice.
- A new probe test that feeds heap-allocated `DriftString` (e.g.
  `"HO" + "ME"`) through each converted function and asserts
  memcheck-clean would close the loop — recommend adding one for
  `drift_env_get`/`_has` and `drift_assert_loc` (the previously-masked
  cases) at minimum.

---

## Sequencing inside the slice

Order matters because the audit will fail until every site is
converted or marker-annotated:

1. **Land the macro** — `string_runtime.h` change only; everything still
   compiles.
2. **Convert sites** — `console_runtime.c` (with borrowed-helper
   extraction), `env_runtime.c`, `posix/io_runtime.c`,
   `posix/assert_runtime.c`, `array_runtime.c`.  After each file, run
   memcheck on the relevant e2e cluster (`std_io_*`, `env_*`, etc.) to
   confirm no regression.
3. **Add allow markers** — `string_runtime.c` primitives and
   `drift_bounds_check_fail`.
4. **Land the static audit test** — should pass cleanly given #1-#3.
5. **history.md + version bump.**

Land as a single slice (one PR / one commit family) — splitting risks
landing the audit without the conversions or vice versa, both of which
leave the tree in a transitionally inconsistent state.

---

## Out of scope

- Drift-side compiler changes (those are slice 2 — `materialize_owned_temp`).
- Refactoring `drift_string_concat` / `drift_string_retain` /
  `drift_string_release` themselves — these are the primitives the
  macro builds on; they get allow markers, not conversions.
- Any string ABI change (sizeof DriftString, refcount layout, header
  format).  Pure C ergonomics.
- Audit coverage extension to other extern receivers (e.g. `DriftError`,
  `DriftIface`).  Different ABI rules, different slice.
