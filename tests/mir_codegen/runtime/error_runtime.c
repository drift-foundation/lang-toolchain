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
    return err;
}

const char* error_to_cstr(struct Error* err) {
    if (!err) return NULL;
    if (err->diag) return err->diag;

    const char* event = err->event ? err->event : "unknown";
    const char* domain = err->domain ? err->domain : "main";

    /* Build attrs JSON fragment. */
    size_t attrs_len = 0;
    for (size_t i = 0; i < err->attr_count; i++) {
        const char* k = err->keys && err->keys[i] ? err->keys[i] : "unknown";
        const char* v = err->values && err->values[i] ? err->values[i] : "unknown";
        attrs_len += (size_t)snprintf(NULL, 0, "\"%s\":\"%s\"", k, v);
        if (i + 1 < err->attr_count) attrs_len += 1; /* comma */
    }

    /* Build frames JSON fragment. */
    size_t frames_len = 0;
    for (size_t i = 0; i < err->frame_count; i++) {
        const char* module = (err->frame_modules && err->frame_modules[i]) ? err->frame_modules[i] : "<unknown>";
        const char* file = (err->frame_files && err->frame_files[i]) ? err->frame_files[i] : "<unknown>";
        const char* func = (err->frame_funcs && err->frame_funcs[i]) ? err->frame_funcs[i] : "<unknown>";
        long long line = (err->frame_lines) ? (long long)err->frame_lines[i] : 0;
        frames_len += (size_t)snprintf(NULL, 0,
            "{\"module\":\"%s\",\"file\":\"%s\",\"func\":\"%s\",\"line\":%lld}",
            module, file, func, line);
        if (i + 1 < err->frame_count) frames_len += 1; /* comma */
    }

    char* attrs_buf = NULL;
    char* frames_buf = NULL;
    if (attrs_len > 0) {
        attrs_buf = (char*)malloc(attrs_len + 1);
        if (!attrs_buf) { free(err->diag); err->diag = NULL; return NULL; }
        char* p = attrs_buf;
        for (size_t i = 0; i < err->attr_count; i++) {
            const char* k = err->keys && err->keys[i] ? err->keys[i] : "unknown";
            const char* v = err->values && err->values[i] ? err->values[i] : "unknown";
            p += snprintf(p, attrs_len - (size_t)(p - attrs_buf) + 1, "\"%s\":\"%s\"", k, v);
            if (i + 1 < err->attr_count) *p++ = ',';
        }
        *p = '\0';
    }
    if (frames_len > 0) {
        frames_buf = (char*)malloc(frames_len + 1);
        if (!frames_buf) { free(err->diag); err->diag = NULL; free(attrs_buf); return NULL; }
        char* p = frames_buf;
        for (size_t i = 0; i < err->frame_count; i++) {
            const char* module = (err->frame_modules && err->frame_modules[i]) ? err->frame_modules[i] : "<unknown>";
            const char* file = (err->frame_files && err->frame_files[i]) ? err->frame_files[i] : "<unknown>";
            const char* func = (err->frame_funcs && err->frame_funcs[i]) ? err->frame_funcs[i] : "<unknown>";
            long long line = (err->frame_lines) ? (long long)err->frame_lines[i] : 0;
            p += snprintf(p, frames_len - (size_t)(p - frames_buf) + 1,
                "{\"module\":\"%s\",\"file\":\"%s\",\"func\":\"%s\",\"line\":%lld}",
                module, file, func, line);
            if (i + 1 < err->frame_count) *p++ = ',';
        }
        *p = '\0';
    }

    char* attrs_section = NULL;
    if (attrs_len > 0 && attrs_buf) {
        attrs_section = (char*)malloc(attrs_len + 3);
        if (!attrs_section) { free(attrs_buf); free(frames_buf); return NULL; }
        snprintf(attrs_section, attrs_len + 3, "{%s}", attrs_buf);
    } else {
        attrs_section = strdup("{}");
    }

    char* frames_section = NULL;
    if (frames_len > 0 && frames_buf) {
        frames_section = (char*)malloc(frames_len + 3);
        if (!frames_section) { free(attrs_buf); free(frames_buf); free(attrs_section); return NULL; }
        snprintf(frames_section, frames_len + 3, "[%s]", frames_buf);
    } else {
        frames_section = strdup("[]");
    }

    size_t total = (size_t)snprintf(
        NULL,
        0,
        "{\"event\":\"%s\",\"domain\":\"%s\",\"attrs\":%s,\"frames\":%s}",
        event,
        domain,
        attrs_section,
        frames_section);
    total += 1; /* null terminator */
    err->diag = (char*)malloc(total);
    if (!err->diag) {
        free(attrs_buf);
        free(frames_buf);
        return NULL;
    }
    snprintf(err->diag, total,
        "{\"event\":\"%s\",\"domain\":\"%s\",\"attrs\":%s,\"frames\":%s}",
        event,
        domain,
        attrs_section,
        frames_section);

    free(attrs_buf);
    free(frames_buf);
    free(attrs_section);
    free(frames_section);
    return err->diag;
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
