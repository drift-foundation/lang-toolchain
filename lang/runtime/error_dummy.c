// Minimal dummy error constructor for SSA error-path testing.
#include "error_dummy.h"
#include <stdlib.h>

struct DriftError* drift_error_new_dummy(int64_t code, struct DriftString payload) {
    struct DriftError* err = malloc(sizeof(struct DriftError));
    if (!err) {
        abort();
    }
    err->code = code;
    err->payload = payload;
    return err;
}

int64_t drift_error_get_code(struct DriftError* err) {
    if (!err) return 0;
    return err->code;
}
