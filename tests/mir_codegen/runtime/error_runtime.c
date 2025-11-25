#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include "error_runtime.h"

struct Error* drift_error_new(
    DriftStr* keys,
    DriftStr* values,
    size_t attr_count,
    DriftStr event,
    DriftStr domain,
    DriftStr* frame_files,
    DriftStr* frame_funcs,
    int64_t* frame_lines,
    size_t frame_count) {
    struct Error* err = (struct Error*)calloc(1, sizeof(struct Error));
    if (!err) return NULL;
    err->event = event ? event : "unknown";
    err->domain = domain ? domain : "main";
    err->attr_count = attr_count;
    err->keys = keys;
    err->values = values;
    err->frame_files = frame_files;
    err->frame_funcs = frame_funcs;
    err->frame_lines = frame_lines;
    err->frame_count = frame_count;
    /* Build a diagnostic string including all attrs. */
    size_t total = 2; // {}
    for (size_t i = 0; i < attr_count; i++) {
        const char* k = err->keys[i] ? err->keys[i] : "unknown";
        const char* v = err->values[i] ? err->values[i] : "unknown";
        total += strlen(k) + strlen(v) + 7; // quotes, colon, comma
    }
    err->diag = (char*)malloc(total + 1);
    if (!err->diag) {
        free(err);
        return NULL;
    }
    char* p = err->diag;
    *p++ = '{';
    for (size_t i = 0; i < attr_count; i++) {
        const char* k = err->keys[i] ? err->keys[i] : "unknown";
        const char* v = err->values[i] ? err->values[i] : "unknown";
        int n = snprintf(p, total - (p - err->diag), "\"%s\":\"%s\"", k, v);
        p += n;
        if (i + 1 < attr_count) {
            *p++ = ',';
        }
    }
    *p++ = '}';
    *p = '\0';
    return err;
}

const char* error_to_cstr(struct Error* err) {
    if (!err) return NULL;
    if (err->diag) return err->diag;
    return "{\"msg\":\"unknown\"}";
}

void error_free(struct Error* err) {
    if (!err) return;
    free(err->diag);
    free(err);
}

struct Error* error_new(const char* msg) {
    static const char* keys[1] = {"msg"};
    const char* vals_arr[1];
    vals_arr[0] = msg ? msg : "unknown";
    /* Casting away const for simplicity; in real runtime we'd copy or enforce const. */
    return drift_error_new((DriftStr*)keys, (DriftStr*)vals_arr, 1, "Error", "main", NULL, NULL, NULL, 0);
}
