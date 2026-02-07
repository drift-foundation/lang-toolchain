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

#endif  // DRIFT_STRING_RUNTIME_H
