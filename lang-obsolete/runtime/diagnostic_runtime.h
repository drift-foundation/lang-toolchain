// DiagnosticValue runtime support (SSA backend)
#pragma once

#include <stdint.h>
#include <stddef.h>

#include "string_runtime.h"

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
            int64_t len;
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

struct DriftOptionalInt {
    uint8_t is_some;
    int64_t value;
};

struct DriftOptionalBool {
    uint8_t is_some;
    uint8_t value;
};

struct DriftOptionalFloat {
    uint8_t is_some;
    double value;
};

struct DriftOptionalString {
    uint8_t is_some;
    struct DriftString value;
};

static const struct DriftOptionalInt OPTIONAL_INT_NONE = {0, 0};
static const struct DriftOptionalBool DRIFT_OPTIONAL_BOOL_NONE = {0, 0};
static const struct DriftOptionalFloat DRIFT_OPTIONAL_FLOAT_NONE = {0, 0.0};
static const struct DriftOptionalString OPTIONAL_STRING_NONE = {0, {0, NULL}};

// Constructors
struct DriftDiagnosticValue drift_dv_missing(void);
struct DriftDiagnosticValue drift_dv_null(void);
struct DriftDiagnosticValue drift_dv_bool(uint8_t value);
struct DriftDiagnosticValue drift_dv_int(int64_t value);
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
struct DriftOptionalInt drift_dv_as_int(const struct DriftDiagnosticValue* dv);
struct DriftOptionalBool drift_dv_as_bool(const struct DriftDiagnosticValue* dv);
struct DriftOptionalFloat drift_dv_as_float(const struct DriftDiagnosticValue* dv);
struct DriftOptionalString drift_dv_as_string(const struct DriftDiagnosticValue* dv);

// Primitive to_diag helpers (runtime equivalents of Diagnostic for primitives)
struct DriftDiagnosticValue drift_diag_from_bool(uint8_t value);
struct DriftDiagnosticValue drift_diag_from_int(int64_t value);
struct DriftDiagnosticValue drift_diag_from_float(double value);
struct DriftDiagnosticValue drift_diag_from_string(struct DriftString value);
struct DriftDiagnosticValue drift_diag_from_optional_int(struct DriftOptionalInt opt);
struct DriftDiagnosticValue drift_diag_from_optional_string(struct DriftOptionalString opt);
struct DriftDiagnosticValue drift_diag_from_optional_bool(struct DriftOptionalBool opt);
struct DriftDiagnosticValue drift_diag_from_optional_float(struct DriftOptionalFloat opt);

#ifdef __cplusplus
}
#endif
