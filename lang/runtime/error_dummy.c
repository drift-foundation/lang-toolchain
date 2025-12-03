// Minimal dummy error constructor for SSA error-path testing.
#include "error_dummy.h"
#include <stdlib.h>
#include <stdio.h>

struct DriftError* drift_error_new_dummy(int64_t code, struct DriftString key, struct DriftString payload) {
    struct DriftError* err = malloc(sizeof(struct DriftError));
    if (!err) {
        abort();
    }
    err->code = code;
    err->payload = payload;
    err->args = NULL;
    err->arg_count = 0;
    if (key.len > 0) {
        err->args = (struct DriftErrorArg*)malloc(sizeof(struct DriftErrorArg));
        if (!err->args) {
            free(err);
            abort();
        }
        err->arg_count = 1;
        err->args[0].key = key;
        err->args[0].value = payload;
    }
    return err;
}

void drift_error_add_arg(struct DriftError* err, struct DriftString key, struct DriftString value) {
    if (!err) {
        return;
    }
    size_t new_count = err->arg_count + 1;
    struct DriftErrorArg* new_args = realloc(err->args, new_count * sizeof(struct DriftErrorArg));
    if (!new_args) {
        abort();
    }
    new_args[new_count - 1].key = key;
    new_args[new_count - 1].value = value;
    err->args = new_args;
    err->arg_count = new_count;
}

int64_t drift_error_get_code(struct DriftError* err) {
    if (!err) return 0;
    return err->code;
}

struct DriftOptionalString __exc_args_get(const struct DriftError* err, struct DriftString key) {
    struct DriftOptionalString out = OPTIONAL_STRING_NONE;
    if (!err) {
        return out;
    }
    const struct DriftString* val = drift_error_get_arg(err, &key);
    if (!val) {
        return out;
    }
    out.is_some = 1;
    out.value = *val;
    return out;
}

const struct DriftString* drift_error_get_arg(const struct DriftError* err, const struct DriftString* key) {
    if (!err || !key) return NULL;
    for (size_t i = 0; i < err->arg_count; i++) {
        struct DriftErrorArg* entry = &err->args[i];
        if (drift_string_eq(entry->key, *key)) {
            return &entry->value;
        }
    }
    return NULL;
}

struct DriftString __exc_args_get_required(const struct DriftError* err, struct DriftString key) {
    struct DriftString empty = {0, NULL};
    if (!err) return empty;
    const struct DriftString* val = drift_error_get_arg(err, &key);
    if (!val) return empty;
    return *val;
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
