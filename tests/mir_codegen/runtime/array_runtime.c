#include "array_runtime.h"

#include <stdio.h>
#include <stdlib.h>
#include <limits.h>
#include <string.h>

void* drift_alloc_array(size_t elem_size, size_t elem_align, drift_size len, drift_size cap) {
    (void)elem_align; /* alignment is currently unused */
    if (cap < len) {
        cap = len;
    }
    if (cap && elem_size > SIZE_MAX / cap) {
        abort();
    }
    size_t bytes = (size_t)cap * elem_size;
    void* buf = malloc(bytes ? bytes : 1);
    if (!buf) {
        abort();
    }
    return buf;
}

void drift_bounds_check_fail(drift_size idx, drift_size len) {
    struct DriftString keys[2];
    struct DriftString vals[2];
    keys[0] = drift_string_literal("container", 9);
    keys[1] = drift_string_literal("index", 5);
    vals[0] = drift_string_literal("Array", 5);
    vals[1] = drift_string_from_int64((int64_t)idx);

    struct DriftString event = drift_string_literal("IndexError", 10);
    struct DriftString domain = drift_string_literal("lang.array", 10);

    struct Error* err = drift_error_new(
        keys,
        vals,
        2,
        &event,
        &domain,
        NULL,
        NULL,
        NULL,
        NULL,
        0,
        NULL,
        NULL,
        NULL,
        0);

    if (err) {
        const char* msg = error_to_cstr(err);
        if (msg) {
            fputs(msg, stderr);
            fputc('\n', stderr);
        }
        error_free(err);
    }
    exit(1);
}
