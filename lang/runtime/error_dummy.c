// Minimal dummy error constructor for SSA error-path testing.
#include "error_dummy.h"
#include <stdlib.h>

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

int64_t drift_error_get_code(struct DriftError* err) {
    if (!err) return 0;
    return err->code;
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
