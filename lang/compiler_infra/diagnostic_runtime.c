#include "diagnostic_runtime.h"
#include <stdlib.h>
#include <string.h>
#include <stdbool.h>

// Array buffers are allocated via drift_alloc_array in generated code.
// When consuming entry arrays into object storage, free the source buffer
// through the matching runtime API.
extern void drift_free_array(void* data);
extern struct DriftString drift_string_retain(struct DriftString s);
extern void drift_string_release(struct DriftString s);

static struct DriftString dv_string_to_drift_string(const struct DriftDiagnosticValue* dv) {
    struct DriftString s;
    s.len = dv->data.string_value.len;
    s.data = dv->data.string_value.data;
    return s;
}

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
    struct DriftString retained = drift_string_retain(value);
    dv.data.string_value.len = retained.len;
    dv.data.string_value.data = retained.data;
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

struct DriftDiagnosticValue drift_dv_object_from_entries(void* entries_data, drift_isize len) {
    if (entries_data == NULL || len <= 0) {
        return drift_dv_object(NULL, 0);
    }
    size_t count = (size_t)len;
    struct DriftDiagnosticField* fields = (struct DriftDiagnosticField*)calloc(count, sizeof(struct DriftDiagnosticField));
    if (!fields) {
        abort();
    }
    struct DriftDiagnosticEntry* entries = (struct DriftDiagnosticEntry*)entries_data;
    for (size_t i = 0; i < count; i++) {
        fields[i].key = entries[i].key;
        fields[i].value = entries[i].value;
    }
    // Entries are consumed by move into `fields`; free only backing storage.
    drift_free_array(entries_data);
    return drift_dv_object(fields, count);
}

static struct DriftDiagnosticValue drift_dv_clone_impl(const struct DriftDiagnosticValue* src, int depth) {
    if (!src || depth > 64) {
        return drift_dv_missing();
    }
    switch (src->tag) {
        case DV_STRING:
            return drift_dv_string(dv_string_to_drift_string(src));
        case DV_OBJECT: {
            size_t count = src->data.object.len;
            if (count == 0 || src->data.object.fields == NULL) {
                return drift_dv_object(NULL, 0);
            }
            struct DriftDiagnosticField* fields = (struct DriftDiagnosticField*)calloc(count, sizeof(struct DriftDiagnosticField));
            if (!fields) {
                abort();
            }
            for (size_t i = 0; i < count; i++) {
                const struct DriftDiagnosticField* in_f = &src->data.object.fields[i];
                fields[i].key = drift_string_retain(in_f->key);
                fields[i].value = drift_dv_clone_impl(&in_f->value, depth + 1);
            }
            return drift_dv_object(fields, count);
        }
        default:
            return *src;
    }
}

struct DriftDiagnosticValue drift_dv_clone(const struct DriftDiagnosticValue* dv) {
    return drift_dv_clone_impl(dv, 0);
}

static void drift_dv_release_impl(struct DriftDiagnosticValue* dv, int depth) {
    if (!dv || depth > 64) {
        return;
    }
    switch (dv->tag) {
        case DV_STRING:
            drift_string_release(dv_string_to_drift_string(dv));
            break;
        case DV_OBJECT: {
            struct DriftDiagnosticObject obj = dv->data.object;
            for (size_t i = 0; i < obj.len; i++) {
                struct DriftDiagnosticField* field = &obj.fields[i];
                drift_string_release(field->key);
                drift_dv_release_impl(&field->value, depth + 1);
            }
            free(obj.fields);
            dv->data.object.fields = NULL;
            dv->data.object.len = 0;
            break;
        }
        default:
            break;
    }
    *dv = drift_dv_missing();
}

void drift_dv_release(struct DriftDiagnosticValue* dv) {
    drift_dv_release_impl(dv, 0);
}

struct DriftDiagnosticValue drift_dv_get(struct DriftDiagnosticValue dv, struct DriftString field) {
    if (dv.tag != DV_OBJECT) {
        return drift_dv_missing();
    }
    for (size_t i = dv.data.object.len; i > 0; i--) {
        size_t idx = i - 1;
        struct DriftDiagnosticField* f = &dv.data.object.fields[idx];
        if (f->key.len == field.len && (field.len == 0 || memcmp(f->key.data, field.data, (size_t)field.len) == 0)) {
            return f->value;
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
        // Return an unowned reference.  The caller (codegen Optional<String>
        // construction) is responsible for retaining if it needs ownership.
        *out = dv_string_to_drift_string(dv);
    }
    return true;
}

bool drift_dv_as_object(const struct DriftDiagnosticValue* dv, struct DriftDiagnosticValue* out) {
    if (!dv || dv->tag != DV_OBJECT) {
        return false;
    }
    if (out) {
        *out = drift_dv_clone(dv);
    }
    return true;
}

bool drift_dv_get_field(const struct DriftDiagnosticValue* dv, struct DriftString key, struct DriftDiagnosticValue* out) {
    if (out) {
        *out = drift_dv_missing();
    }
    if (!dv || dv->tag != DV_OBJECT) {
        return false;
    }
    struct DriftDiagnosticValue got = drift_dv_get(*dv, key);
    if (got.tag == DV_MISSING) {
        return false;
    }
    if (out) {
        *out = drift_dv_clone(&got);
    }
    return true;
}

struct DriftDiagnosticValue drift_diag_from_bool(uint8_t value) { return drift_dv_bool(value); }
struct DriftDiagnosticValue drift_diag_from_int(drift_isize value) { return drift_dv_int(value); }
struct DriftDiagnosticValue drift_diag_from_float(double value) { return drift_dv_float(value); }
struct DriftDiagnosticValue drift_diag_from_string(struct DriftString value) { return drift_dv_string(value); }
