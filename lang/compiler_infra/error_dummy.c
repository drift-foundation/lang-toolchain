#include "error_dummy.h"
#include <stdlib.h>
#include <stdio.h>
#include <string.h>

struct DriftError* drift_error_new_dummy(drift_error_code_t code, struct DriftString event_fqn, struct DriftString key, struct DriftString payload) {
    struct DriftError* err = malloc(sizeof(struct DriftError));
    if (!err) {
        abort();
    }
    err->code = code;
    err->event_fqn = event_fqn;
    err->attrs = NULL;
    err->attr_count = 0;
    err->frames = NULL;
    err->frame_count = 0;
    (void)key;
    (void)payload;
    return err;
}

void drift_error_add_attr_dv(struct DriftError* err, struct DriftString key, const struct DriftDiagnosticValue* value) {
    if (!err) {
        return;
    }
    size_t new_count = err->attr_count + 1;
    struct DriftErrorAttr* new_attrs = realloc(err->attrs, new_count * sizeof(struct DriftErrorAttr));
    if (!new_attrs) {
        abort();
    }
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
    size_t frame_idx = err->frame_count;
    for (size_t i = 0; i < err->frame_count; i++) {
        if (err->frames[i].name.len == frame.len && (frame.len == 0 || memcmp(err->frames[i].name.data, frame.data, frame.len) == 0)) {
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

drift_error_code_t drift_error_get_code(struct DriftError* err) {
    if (!err) return 0;
    return err->code;
}

struct DriftString drift_error_get_event_fqn(const struct DriftError* err) {
    if (!err) {
        struct DriftString empty = {0, NULL};
        return empty;
    }
    return err->event_fqn;
}

const struct DriftDiagnosticValue* drift_error_get_attr(const struct DriftError* err, const struct DriftString* key) {
    if (!err || !key) return NULL;
    for (size_t i = 0; i < err->attr_count; i++) {
        struct DriftErrorAttr* entry = &err->attrs[i];
        if (entry->key.len == key->len && (key->len == 0 || memcmp(entry->key.data, key->data, key->len) == 0)) {
            return &entry->value;
        }
    }
    return NULL;
}

uint8_t __exc_attrs_get(struct DriftString* out, const struct DriftError* err, struct DriftString key) {
    if (!err) {
        return 0;
    }
    const struct DriftDiagnosticValue* val = drift_error_get_attr(err, &key);
    if (!val || val->tag != DV_STRING) {
        return 0;
    }
    if (out) {
        out->len = val->data.string_value.len;
        out->data = val->data.string_value.data;
    }
    return 1;
}

void __exc_attrs_get_dv(struct DriftDiagnosticValue* out, const struct DriftError* err, struct DriftString key) {
    if (!out) return;
    *out = drift_dv_missing();
    if (!err) {
        return;
    }
    const struct DriftDiagnosticValue* val = drift_error_get_attr(err, &key);
    if (!val) {
        return;
    }
    *out = *val;
}

struct DriftError* drift_error_new_with_payload(drift_error_code_t code, struct DriftString event_fqn, struct DriftString key, const struct DriftDiagnosticValue* payload) {
    struct DriftError* err = drift_error_new_dummy(code, event_fqn, (struct DriftString){0, NULL}, (struct DriftString){0, NULL});
    if (!err) {
        return NULL;
    }
    if (payload) {
        drift_error_add_attr_dv(err, key, payload);
    }
    return err;
}

struct DriftError* drift_error_new(drift_error_code_t code, struct DriftString event_fqn) {
    struct DriftError* err = drift_error_new_dummy(code, event_fqn, (struct DriftString){0, NULL}, (struct DriftString){0, NULL});
    return err;
}

void drift_error_raise(struct DriftError* err) {
    (void)err;
    abort();
}
