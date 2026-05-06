#pragma once

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

enum DriftDiagnosticTag {
    DV_MISSING = 0,
    DV_NULL = 1,
    DV_BOOL = 2,
    DV_INT = 3,
    DV_FLOAT = 4,
    DV_STRING = 5,
    DV_ARRAY = 6,
    DV_OBJECT = 7,
};

typedef ptrdiff_t drift_isize;
typedef size_t drift_usize;

_Static_assert(sizeof(drift_isize) == sizeof(void*), "drift_isize must be pointer-sized");
_Static_assert(sizeof(drift_usize) == sizeof(void*), "drift_usize must be pointer-sized");

#ifndef DRIFT_STRING_RUNTIME_H
struct DriftString {
    drift_isize len;
    char* data;
};
#endif

struct DriftDiagnosticArray {
    struct DriftDiagnosticValue* items;
    size_t len;
};

struct DriftDiagnosticField;

struct DriftDiagnosticObject {
    struct DriftDiagnosticField* fields;
    size_t len;
};

struct DriftDiagnosticValue {
    uint8_t tag; // DriftDiagnosticTag
    uint8_t _pad[7]; // pad to 8-byte boundary
    union {
        uint64_t as_u64[2]; // 16-byte blob for generic storage
        struct {
            drift_isize len;
            char* data;
        } string_value;
        int64_t int_value;
        double float_value;
        uint8_t bool_value;
        struct DriftDiagnosticArray array;
        struct DriftDiagnosticObject object;
    } data;
};

struct DriftDiagnosticField {
    struct DriftString key;
    struct DriftDiagnosticValue value;
};

struct DriftDiagnosticEntry {
    struct DriftString key;
    struct DriftDiagnosticValue value;
};

_Static_assert(sizeof(struct DriftDiagnosticValue) == 24, "DriftDiagnosticValue size mismatch");
_Static_assert(_Alignof(struct DriftDiagnosticValue) == 8, "DriftDiagnosticValue alignment mismatch");

// Slice 7c-1 (ABI 14, 2026-05-06): all `drift_dv_*` function
// declarations deleted.  The struct types above remain declared
// for transitive includers but carry no callable surface at
// ABI 14 — no production code path references DV.

// Slice 7c-1 (ABI 14, 2026-05-06): the `drift_diag_from_*`
// aliases (bool/int/float/string) are deleted alongside the
// `drift_dv_*` family — no callable surface remains.
//
// Layout asserts on `DriftDiagnosticEntry` are also retired here:
// they were boundary-critical only for `drift_dv_entries`, which
// is gone.  Slice 7c-2 deletes the struct types themselves
// alongside the compiler-internal `TypeKind.DIAGNOSTICVALUE`.

#ifdef __cplusplus
}
#endif
