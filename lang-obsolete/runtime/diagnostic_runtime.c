// DiagnosticValue runtime support (SSA backend)

#include "diagnostic_runtime.h"
#include <stdio.h>

struct DriftDiagnosticValue drift_dv_missing(void) {
    struct DriftDiagnosticValue dv = {0};
    dv.tag = DV_MISSING;
    return dv;
}

struct DriftDiagnosticValue drift_dv_null(void) {
    struct DriftDiagnosticValue dv = {0};
    dv.tag = DV_NULL;
    return dv;
}

struct DriftDiagnosticValue drift_dv_bool(uint8_t value) {
    struct DriftDiagnosticValue dv = {0};
    dv.tag = DV_BOOL;
    dv.data.bool_value = value ? 1 : 0;
    return dv;
}

struct DriftDiagnosticValue drift_dv_int(int64_t value) {
    struct DriftDiagnosticValue dv = {0};
    dv.tag = DV_INT;
    dv.data.int_value = value;
    return dv;
}

struct DriftDiagnosticValue drift_dv_float(double value) {
    struct DriftDiagnosticValue dv = {0};
    dv.tag = DV_FLOAT;
    dv.data.float_value = value;
    return dv;
}

struct DriftDiagnosticValue drift_dv_string(struct DriftString value) {
    struct DriftDiagnosticValue dv = {0};
    dv.tag = DV_STRING;
    dv.data.string_value.len = value.len;
    dv.data.string_value.data = value.data;
    return dv;
}

struct DriftDiagnosticValue drift_dv_array(struct DriftDiagnosticValue* items, size_t len) {
    struct DriftDiagnosticValue dv = {0};
    dv.tag = DV_ARRAY;
    dv.data.array.items = items;
    dv.data.array.len = len;
    return dv;
}

struct DriftDiagnosticValue drift_dv_object(struct DriftDiagnosticField* fields, size_t len) {
    struct DriftDiagnosticValue dv = {0};
    dv.tag = DV_OBJECT;
    dv.data.object.fields = fields;
    dv.data.object.len = len;
    return dv;
}

struct DriftDiagnosticValue drift_dv_get(struct DriftDiagnosticValue dv, struct DriftString field) {
    if (dv.tag != DV_OBJECT) {
        return drift_dv_missing();
    }
    for (size_t i = 0; i < dv.data.object.len; i++) {
        struct DriftDiagnosticField* f = &dv.data.object.fields[i];
        if (drift_string_eq(f->key, field)) {
            return *(f->value);
        }
    }
    return drift_dv_missing();
}

struct DriftDiagnosticValue drift_dv_index(struct DriftDiagnosticValue dv, size_t idx) {
    if (dv.tag != DV_ARRAY) {
        return drift_dv_missing();
    }
    if (idx >= dv.data.array.len) {
        return drift_dv_missing();
    }
    return dv.data.array.items[idx];
}

uint8_t drift_dv_kind(struct DriftDiagnosticValue dv) {
    return dv.tag;
}

struct DriftOptionalInt drift_dv_as_int(const struct DriftDiagnosticValue* dv) {
    if (!dv || dv->tag != DV_INT) {
        return OPTIONAL_INT_NONE;
    }
    struct DriftOptionalInt out = {1, dv->data.int_value};
    return out;
}

struct DriftOptionalBool drift_dv_as_bool(const struct DriftDiagnosticValue* dv) {
    if (!dv || dv->tag != DV_BOOL) {
        return DRIFT_OPTIONAL_BOOL_NONE;
    }
    struct DriftOptionalBool out = {1, (uint8_t)(dv->data.bool_value ? 1 : 0)};
    return out;
}

struct DriftOptionalFloat drift_dv_as_float(const struct DriftDiagnosticValue* dv) {
    if (!dv || dv->tag != DV_FLOAT) {
        return DRIFT_OPTIONAL_FLOAT_NONE;
    }
    struct DriftOptionalFloat out = {1, dv->data.float_value};
    return out;
}

struct DriftOptionalString drift_dv_as_string(const struct DriftDiagnosticValue* dv) {
    if (!dv || dv->tag != DV_STRING) {
        return OPTIONAL_STRING_NONE;
    }
    struct DriftOptionalString out = {1, {dv->data.string_value.len, dv->data.string_value.data}};
    return out;
}

struct DriftDiagnosticValue drift_diag_from_bool(uint8_t value) {
    return drift_dv_bool(value);
}

struct DriftDiagnosticValue drift_diag_from_int(int64_t value) {
    return drift_dv_int(value);
}

struct DriftDiagnosticValue drift_diag_from_float(double value) {
    return drift_dv_float(value);
}

struct DriftDiagnosticValue drift_diag_from_string(struct DriftString value) {
    return drift_dv_string(value);
}

struct DriftDiagnosticValue drift_diag_from_optional_int(struct DriftOptionalInt opt) {
    if (!opt.is_some) {
        return drift_dv_null();
    }
    return drift_dv_int(opt.value);
}

struct DriftDiagnosticValue drift_diag_from_optional_string(struct DriftOptionalString opt) {
    if (!opt.is_some) {
        return drift_dv_null();
    }
    return drift_dv_string(opt.value);
}

struct DriftDiagnosticValue drift_diag_from_optional_bool(struct DriftOptionalBool opt) {
    if (!opt.is_some) {
        return drift_dv_null();
    }
    return drift_dv_bool(opt.value);
}

struct DriftDiagnosticValue drift_diag_from_optional_float(struct DriftOptionalFloat opt) {
    if (!opt.is_some) {
        return drift_dv_null();
    }
    return drift_dv_float(opt.value);
}
