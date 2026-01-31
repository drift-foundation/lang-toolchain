#include "diagnostic_runtime.h"
#include <stdlib.h>
#include <string.h>
#include <stdbool.h>

static struct DriftDiagnosticValue make_simple(uint8_t tag) {
    struct DriftDiagnosticValue dv;
    dv.tag = tag;
    memset(dv._pad, 0, sizeof(dv._pad));
    dv.data.as_u64[0] = 0;
    dv.data.as_u64[1] = 0;
    return dv;
}

struct DriftDiagnosticValue drift_dv_missing(void) { return make_simple(DV_MISSING); }
struct DriftDiagnosticValue drift_dv_null(void) { return make_simple(DV_NULL); }

struct DriftDiagnosticValue drift_dv_bool(uint8_t value) {
    struct DriftDiagnosticValue dv = make_simple(DV_BOOL);
    dv.data.bool_value = value ? 1 : 0;
    return dv;
}

struct DriftDiagnosticValue drift_dv_int(drift_isize value) {
    struct DriftDiagnosticValue dv = make_simple(DV_INT);
    dv.data.int_value = value;
    return dv;
}

struct DriftDiagnosticValue drift_dv_float(double value) {
    struct DriftDiagnosticValue dv = make_simple(DV_FLOAT);
    dv.data.float_value = value;
    return dv;
}

struct DriftDiagnosticValue drift_dv_string(struct DriftString value) {
    struct DriftDiagnosticValue dv = make_simple(DV_STRING);
    dv.data.string_value.len = value.len;
    dv.data.string_value.data = value.data;
    return dv;
}

struct DriftDiagnosticValue drift_dv_array(struct DriftDiagnosticValue* items, size_t len) {
    struct DriftDiagnosticValue dv = make_simple(DV_ARRAY);
    dv.data.array.items = items;
    dv.data.array.len = len;
    return dv;
}

struct DriftDiagnosticValue drift_dv_object(struct DriftDiagnosticField* fields, size_t len) {
    struct DriftDiagnosticValue dv = make_simple(DV_OBJECT);
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
        if (f->key.len == field.len && (field.len == 0 || memcmp(f->key.data, field.data, (size_t)field.len) == 0)) {
            if (f->value) {
                return *(f->value);
            }
            break;
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

uint8_t drift_dv_kind(struct DriftDiagnosticValue dv) { return dv.tag; }

bool drift_dv_as_int(const struct DriftDiagnosticValue* dv, drift_isize* out) {
    if (!dv || dv->tag != DV_INT) {
        return false;
    }
    if (out) {
        *out = (drift_isize)dv->data.int_value;
    }
    return true;
}

bool drift_dv_as_bool(const struct DriftDiagnosticValue* dv, uint8_t* out) {
    if (!dv || dv->tag != DV_BOOL) {
        return false;
    }
    if (out) {
        *out = (uint8_t)(dv->data.bool_value ? 1 : 0);
    }
    return true;
}

bool drift_dv_as_float(const struct DriftDiagnosticValue* dv, double* out) {
    if (!dv || dv->tag != DV_FLOAT) {
        return false;
    }
    if (out) {
        *out = dv->data.float_value;
    }
    return true;
}

bool drift_dv_as_string(const struct DriftDiagnosticValue* dv, struct DriftString* out) {
    if (!dv || dv->tag != DV_STRING) {
        return false;
    }
    if (out) {
        out->len = dv->data.string_value.len;
        out->data = dv->data.string_value.data;
    }
    return true;
}

struct DriftDiagnosticValue drift_diag_from_bool(uint8_t value) { return drift_dv_bool(value); }
struct DriftDiagnosticValue drift_diag_from_int(drift_isize value) { return drift_dv_int(value); }
struct DriftDiagnosticValue drift_diag_from_float(double value) { return drift_dv_float(value); }
struct DriftDiagnosticValue drift_diag_from_string(struct DriftString value) { return drift_dv_string(value); }
