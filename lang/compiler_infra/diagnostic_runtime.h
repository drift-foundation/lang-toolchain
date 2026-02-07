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

struct DriftString {
    drift_isize len;
    char* data;
};

struct DriftDiagnosticArray {
    struct DriftDiagnosticValue* items;
    size_t len;
};

struct DriftDiagnosticField {
    struct DriftString key;
    struct DriftDiagnosticValue* value;
};

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

_Static_assert(sizeof(struct DriftDiagnosticValue) == 24, "DriftDiagnosticValue size mismatch");
_Static_assert(_Alignof(struct DriftDiagnosticValue) == 8, "DriftDiagnosticValue alignment mismatch");

// Constructors
struct DriftDiagnosticValue drift_dv_missing(void);
struct DriftDiagnosticValue drift_dv_null(void);
struct DriftDiagnosticValue drift_dv_bool(uint8_t value);
struct DriftDiagnosticValue drift_dv_int(drift_isize value);
struct DriftDiagnosticValue drift_dv_float(double value);
struct DriftDiagnosticValue drift_dv_string(struct DriftString value);
struct DriftDiagnosticValue drift_dv_array(struct DriftDiagnosticValue* items, size_t len);
struct DriftDiagnosticValue drift_dv_object(struct DriftDiagnosticField* fields, size_t len);

// Accessors
struct DriftDiagnosticValue drift_dv_get(struct DriftDiagnosticValue dv, struct DriftString field);
struct DriftDiagnosticValue drift_dv_index(struct DriftDiagnosticValue dv, size_t idx);

// Type queries
uint8_t drift_dv_kind(struct DriftDiagnosticValue dv);

// Conversions
bool drift_dv_as_int(const struct DriftDiagnosticValue* dv, drift_isize* out);
bool drift_dv_as_bool(const struct DriftDiagnosticValue* dv, uint8_t* out);
bool drift_dv_as_float(const struct DriftDiagnosticValue* dv, double* out);
bool drift_dv_as_string(const struct DriftDiagnosticValue* dv, struct DriftString* out);

// Primitive to_diag helpers (runtime equivalents of Diagnostic for primitives)
struct DriftDiagnosticValue drift_diag_from_bool(uint8_t value);
struct DriftDiagnosticValue drift_diag_from_int(drift_isize value);
struct DriftDiagnosticValue drift_diag_from_float(double value);
struct DriftDiagnosticValue drift_diag_from_string(struct DriftString value);

// DV_MISSING must remain zero so codegen can treat zeroinitializer as the
// canonical missing value when calling drift_dv_missing().
_Static_assert(DV_MISSING == 0, "DV_MISSING must be zero for codegen assumptions");

#ifdef __cplusplus
}
#endif
