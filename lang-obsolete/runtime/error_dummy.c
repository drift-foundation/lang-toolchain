// Minimal dummy error constructor for SSA error-path testing.
#include "error_dummy.h"
#include <stdlib.h>
#include <stdio.h>
#include "diagnostic_runtime.h"

struct DriftError* drift_error_new_dummy(int64_t code, struct DriftString key, struct DriftString payload) {
    struct DriftError* err = malloc(sizeof(struct DriftError));
    if (!err) {
        abort();
    }
    err->code = code;
    err->attrs = NULL;
    err->attr_count = 0;
    err->frames = NULL;
    err->frame_count = 0;
    return err;
}

void drift_error_add_attr_dv(struct DriftError* err, struct DriftString key, const struct DriftDiagnosticValue* value) {
    if (!err) {
        return;
    }
    // Grow by 1 and append; if realloc fails, abort to avoid partial writes.
    size_t new_count = err->attr_count + 1;
    struct DriftErrorAttr* new_attrs = realloc(err->attrs, new_count * sizeof(struct DriftErrorAttr));
    if (!new_attrs) {
        abort();
    }
    // Copy key/value into the newly appended slot.
    new_attrs[new_count - 1].key = key;
    new_attrs[new_count - 1].value = *value;
#ifdef DEBUG_DIAGNOSTICS
    if (value) {
        fprintf(stderr, "[err_add_attr] count=%zu key=%.*s tag=%u len=%lld ptr=%p\n",
                new_count,
                (int)key.len, key.data ? key.data : "<null>",
                value->tag,
                (long long)value->data.string_value.len,
                (void*)value->data.string_value.data);
        fflush(stderr);
    }
#endif
    err->attrs = new_attrs;
    err->attr_count = new_count;
}

void drift_error_add_local_dv(struct DriftError* err, struct DriftString frame, struct DriftString key, struct DriftDiagnosticValue value) {
    if (!err) return;
    // Find or create frame by name.
    size_t frame_idx = err->frame_count;
    for (size_t i = 0; i < err->frame_count; i++) {
        if (drift_string_eq(err->frames[i].name, frame)) {
            frame_idx = i;
            break;
        }
    }
    if (frame_idx == err->frame_count) {
        size_t new_count = err->frame_count + 1;
        struct DriftCtxFrame* new_frames = realloc(err->frames, new_count * sizeof(struct DriftCtxFrame));
        if (!new_frames) abort();
        new_frames[new_count - 1].name = frame;
        new_frames[new_count - 1].locals = NULL;
        new_frames[new_count - 1].local_count = 0;
        err->frames = new_frames;
        err->frame_count = new_count;
    }
    struct DriftCtxFrame* tgt = &err->frames[frame_idx];
    size_t new_lcount = tgt->local_count + 1;
    struct DriftErrorLocal* new_locals = realloc(tgt->locals, new_lcount * sizeof(struct DriftErrorLocal));
    if (!new_locals) abort();
    new_locals[new_lcount - 1].key = key;
    new_locals[new_lcount - 1].value = value;
    tgt->locals = new_locals;
    tgt->local_count = new_lcount;
}

int64_t drift_error_get_code(struct DriftError* err) {
    if (!err) return 0;
    return err->code;
}

const struct DriftDiagnosticValue* drift_error_get_attr(const struct DriftError* err, const struct DriftString* key) {
    if (!err || !key) return NULL;
    for (size_t i = 0; i < err->attr_count; i++) {
        struct DriftErrorAttr* entry = &err->attrs[i];
        if (drift_string_eq(entry->key, *key)) {
            return &entry->value;
        }
    }
    return NULL;
}

// Typed attrs path (string-only for now).
struct DriftOptionalString __exc_attrs_get(const struct DriftError* err, struct DriftString key) {
    struct DriftOptionalString out = OPTIONAL_STRING_NONE;
    if (!err) {
        return out;
    }
    const struct DriftDiagnosticValue* val = drift_error_get_attr(err, &key);
    if (!val || val->tag != DV_STRING) {
        return out;
    }
    out.is_some = 1;
    out.value.len = val->data.string_value.len;
    out.value.data = val->data.string_value.data;
    return out;
}

// Typed DiagnosticValue lookup; writes Missing if absent.
void __exc_attrs_get_dv(struct DriftDiagnosticValue* out, const struct DriftError* err, struct DriftString key) {
    if (!out) return;
    // Defensive default: Missing.
    out->tag = DV_MISSING;
    out->data.int_value = 0;
    if (!err) {
        return;
    }
    // Typed attrs lookup.
    const struct DriftDiagnosticValue* val = drift_error_get_attr(err, &key);
    if (!val) {
        return;
    }
#ifdef DEBUG_DIAGNOSTICS
    fprintf(stderr, "[exc_attrs_get_dv] key=%.*s tag=%u len=%lld ptr=%p\n",
            (int)key.len, key.data ? key.data : "<null>",
            val->tag,
            (long long)val->data.string_value.len,
            (void*)val->data.string_value.data);
    fflush(stderr);
#endif
    *out = *val;
}

struct DriftOptionalInt drift_optional_int_some(int64_t value) {
    struct DriftOptionalInt out;
    out.is_some = 1;
    out.value = value;
    return out;
}

struct DriftOptionalInt drift_optional_int_none(void) {
    return OPTIONAL_INT_NONE;
}
