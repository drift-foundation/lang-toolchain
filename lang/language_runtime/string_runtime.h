#ifndef DRIFT_STRING_RUNTIME_H
#define DRIFT_STRING_RUNTIME_H

#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>

typedef ptrdiff_t drift_isize;

typedef struct DriftString {
	drift_isize len;
	char *data;
} DriftString;

DriftString drift_string_literal(const char *data, drift_isize len);
DriftString drift_string_from_cstr(const char *cstr);
DriftString drift_string_from_utf8_bytes(const char *data, drift_isize len);
DriftString drift_string_from_int64(int64_t v);
DriftString drift_string_from_uint64(uint64_t v);
// Deterministic float formatting (Ryu) for Drift `Float` once the type exists end-to-end.
DriftString drift_string_from_f64(double v);
DriftString drift_string_from_bool(int v);
DriftString drift_string_concat(DriftString a, DriftString b);
int drift_string_eq(DriftString a, DriftString b);
DriftString drift_string_retain(DriftString s);
void drift_string_release(DriftString s);
// DriftString lexicographic comparison by unsigned bytes.
//
// This is a deterministic, locale-independent ordering suitable for the
// language-level `String` comparison operators (`<`, `<=`, `>`, `>=`).
//
// Contract:
//   - Returns <0 if a < b, 0 if a == b, >0 if a > b.
//   - Comparison is lexicographic on the underlying UTF-8 byte sequences
//     (i.e., unsigned byte comparison), with shorter prefix ordering first.
int drift_string_cmp(DriftString a, DriftString b);
void drift_string_free(DriftString s);
char *drift_string_to_cstr(DriftString s);

/* By-value DriftString ABI -- Convention A (normal extern receivers):
 * The Drift caller emits `retain(s); extern(s); release(s)` around
 * the call, transferring an extra refcount stake to the C callee.
 * The callee MUST release that stake exactly once before returning.
 * Annotate received-by-value parameters with DRIFT_OWNED_STRING
 * (using a local copy) to make the release automatic at every scope
 * exit -- no per-return-path drift_string_release() calls needed.
 *
 * Convention B (language built-in / intrinsic receivers) -- do NOT
 * use DRIFT_OWNED_STRING.  The Drift caller passes the existing
 * stake direct (no pre-retain) and releases its own local AFTER the
 * call.  Adding the macro on these sites would double-free (UAF on
 * heap inputs).  Convention-B receivers must instead carry an
 * explicit drift-owned-string-audit allow marker
 * (read-only-borrow / consumed-by-noreturn-callee as appropriate).
 * Current convention-B sites: drift_assert_loc (posix/assert_runtime.c),
 * drift_bounds_check + drift_bounds_check_fail +
 * drift_bounds_check_params_json_build (array_runtime.c).
 *
 * Adoption (both conventions) is enforced by
 * lang/tests/driver/test_drift_owned_string_audit.py, which fails CI
 * if a by-value DriftString receiver lacks either the macro or an
 * explicit drift-owned-string-audit allow marker. */
static inline void _drift_string_cleanup(DriftString *s) {
	drift_string_release(*s);
}
#define DRIFT_OWNED_STRING __attribute__((cleanup(_drift_string_cleanup)))

#endif  // DRIFT_STRING_RUNTIME_H
