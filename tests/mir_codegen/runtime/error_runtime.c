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
    DriftStr* frame_modules,
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
    err->frame_count = frame_count;
    if (frame_count > 0 && frame_modules && frame_files && frame_funcs && frame_lines) {
        err->frame_modules = (DriftStr*)malloc(frame_count * sizeof(DriftStr));
        err->frame_files = (DriftStr*)malloc(frame_count * sizeof(DriftStr));
        err->frame_funcs = (DriftStr*)malloc(frame_count * sizeof(DriftStr));
        err->frame_lines = (int64_t*)malloc(frame_count * sizeof(int64_t));
        if (!err->frame_modules || !err->frame_files || !err->frame_funcs || !err->frame_lines) {
            free(err->frame_modules);
            free(err->frame_files);
            free(err->frame_funcs);
            free(err->frame_lines);
            free(err->diag);
            free(err);
            return NULL;
        }
        for (size_t i = 0; i < frame_count; i++) {
            err->frame_modules[i] = frame_modules[i];
            err->frame_files[i] = frame_files[i];
            err->frame_funcs[i] = frame_funcs[i];
            err->frame_lines[i] = frame_lines[i];
        }
    }
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
    free(err->frame_modules);
    free(err->frame_files);
    free(err->frame_funcs);
    free(err->frame_lines);
    free(err);
}

struct Error* error_new(const char* msg) {
    static const char* keys[1] = {"msg"};
    const char* vals_arr[1];
    vals_arr[0] = msg ? msg : "unknown";
    /* Casting away const for simplicity; in real runtime we'd copy or enforce const. */
    return drift_error_new((DriftStr*)keys, (DriftStr*)vals_arr, 1, "Error", "main", NULL, NULL, NULL, NULL, 0);
}

struct Error* error_push_frame(struct Error* err, DriftStr module, DriftStr file, DriftStr func, int64_t line) {
    if (!err) return NULL;
    size_t new_count = err->frame_count + 1;
    DriftStr* new_modules = (DriftStr*)realloc(err->frame_modules, new_count * sizeof(DriftStr));
    DriftStr* new_files = (DriftStr*)realloc(err->frame_files, new_count * sizeof(DriftStr));
    DriftStr* new_funcs = (DriftStr*)realloc(err->frame_funcs, new_count * sizeof(DriftStr));
    int64_t* new_lines = (int64_t*)realloc(err->frame_lines, new_count * sizeof(int64_t));
    if (!new_modules || !new_files || !new_funcs || !new_lines) {
        return err; // best-effort; leave unchanged on alloc failure
    }
    err->frame_modules = new_modules;
    err->frame_files = new_files;
    err->frame_funcs = new_funcs;
    err->frame_lines = new_lines;
    err->frame_modules[err->frame_count] = module ? module : "<unknown>";
    err->frame_files[err->frame_count] = file ? file : "<unknown>";
    err->frame_funcs[err->frame_count] = func ? func : "<unknown>";
    err->frame_lines[err->frame_count] = line;
    err->frame_count = new_count;
    return err;
}
