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
    size_t frame_count,
    DriftStr* cap_keys,
    DriftStr* cap_values,
    size_t* cap_counts,
    size_t cap_total) {
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
    err->cap_keys = NULL;
    err->cap_values = NULL;
    err->cap_counts = NULL;
    err->cap_total = 0;
    if (frame_count > 0) {
        err->cap_counts = (size_t*)calloc(frame_count, sizeof(size_t));
        if (!err->cap_counts) {
            free(err->frame_modules);
            free(err->frame_files);
            free(err->frame_funcs);
            free(err->frame_lines);
            free(err);
            return NULL;
        }
        if (cap_total > 0 && cap_keys && cap_values && cap_counts) {
            err->cap_keys = (DriftStr*)malloc(cap_total * sizeof(DriftStr));
            err->cap_values = (DriftStr*)malloc(cap_total * sizeof(DriftStr));
            if (!err->cap_keys || !err->cap_values) {
                free(err->cap_keys);
                free(err->cap_values);
                free(err->cap_counts);
                free(err->frame_modules);
                free(err->frame_files);
                free(err->frame_funcs);
                free(err->frame_lines);
                free(err);
                return NULL;
            }
            err->cap_total = cap_total;
            size_t total_seen = 0;
            for (size_t i = 0; i < frame_count; i++) {
                size_t count = cap_counts[i];
                err->cap_counts[i] = count;
                for (size_t j = 0; j < count; j++) {
                    size_t idx = total_seen + j;
                    if (idx < cap_total) {
                        err->cap_keys[idx] = cap_keys[idx];
                        err->cap_values[idx] = cap_values[idx];
                    }
                }
                total_seen += count;
            }
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
    size_t cap_offset = 0;
    for (size_t i = 0; i < err->frame_count; i++) {
        const char* module = (err->frame_modules && err->frame_modules[i]) ? err->frame_modules[i] : "<unknown>";
        const char* file = (err->frame_files && err->frame_files[i]) ? err->frame_files[i] : "<unknown>";
        const char* func = (err->frame_funcs && err->frame_funcs[i]) ? err->frame_funcs[i] : "<unknown>";
        long long line = (err->frame_lines) ? (long long)err->frame_lines[i] : 0;
        size_t cap_count = (err->cap_counts && i < err->frame_count) ? err->cap_counts[i] : 0;
        size_t cap_len = 0;
        for (size_t j = 0; j < cap_count; j++) {
            const char* ck = (err->cap_keys && cap_offset + j < err->cap_total && err->cap_keys[cap_offset + j]) ? err->cap_keys[cap_offset + j] : "unknown";
            const char* cv = (err->cap_values && cap_offset + j < err->cap_total && err->cap_values[cap_offset + j]) ? err->cap_values[cap_offset + j] : "unknown";
            cap_len += (size_t)snprintf(NULL, 0, "\"%s\":\"%s\"", ck, cv);
            if (j + 1 < cap_count) cap_len += 1;
        }
        size_t base_len = (size_t)snprintf(NULL, 0,
            "{\"module\":\"%s\",\"file\":\"%s\",\"func\":\"%s\",\"line\":%lld,\"captured\":{%s}}",
            module, file, func, line, "");
        frames_len += base_len + cap_len;
        if (i + 1 < err->frame_count) frames_len += 1; /* comma */
        cap_offset += cap_count;
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
        size_t cap_offset2 = 0;
        for (size_t i = 0; i < err->frame_count; i++) {
            const char* module = (err->frame_modules && err->frame_modules[i]) ? err->frame_modules[i] : "<unknown>";
            const char* file = (err->frame_files && err->frame_files[i]) ? err->frame_files[i] : "<unknown>";
            const char* func = (err->frame_funcs && err->frame_funcs[i]) ? err->frame_funcs[i] : "<unknown>";
            long long line = (err->frame_lines) ? (long long)err->frame_lines[i] : 0;
            size_t cap_count = (err->cap_counts && i < err->frame_count) ? err->cap_counts[i] : 0;
            /* Build captured fragment inline. */
            size_t cap_len = 0;
            for (size_t j = 0; j < cap_count; j++) {
                const char* ck = (err->cap_keys && cap_offset2 + j < err->cap_total && err->cap_keys[cap_offset2 + j]) ? err->cap_keys[cap_offset2 + j] : "unknown";
                const char* cv = (err->cap_values && cap_offset2 + j < err->cap_total && err->cap_values[cap_offset2 + j]) ? err->cap_values[cap_offset2 + j] : "unknown";
                cap_len += (size_t)snprintf(NULL, 0, "\"%s\":\"%s\"", ck, cv);
                if (j + 1 < cap_count) cap_len += 1;
            }
            char* cap_buf = (char*)malloc(cap_len + 1);
            if (!cap_buf && cap_len > 0) { free(attrs_buf); free(frames_buf); free(err->diag); err->diag = NULL; return NULL; }
            if (cap_len > 0) {
                char* cp = cap_buf;
                for (size_t j = 0; j < cap_count; j++) {
                    const char* ck = (err->cap_keys && cap_offset2 + j < err->cap_total && err->cap_keys[cap_offset2 + j]) ? err->cap_keys[cap_offset2 + j] : "unknown";
                    const char* cv = (err->cap_values && cap_offset2 + j < err->cap_total && err->cap_values[cap_offset2 + j]) ? err->cap_values[cap_offset2 + j] : "unknown";
                    cp += snprintf(cp, cap_len - (size_t)(cp - cap_buf) + 1, "\"%s\":\"%s\"", ck, cv);
                    if (j + 1 < cap_count) *cp++ = ',';
                }
                *cp = '\0';
            } else if (cap_buf) {
                cap_buf[0] = '\0';
            }
            p += snprintf(p, frames_len - (size_t)(p - frames_buf) + 1,
                "{\"module\":\"%s\",\"file\":\"%s\",\"func\":\"%s\",\"line\":%lld,\"captured\":{%s}}",
                module, file, func, line, cap_len > 0 ? cap_buf : "");
            free(cap_buf);
            cap_offset2 += cap_count;
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
    free(err->cap_keys);
    free(err->cap_values);
    free(err->cap_counts);
    free(err);
}

struct Error* error_new(const char* msg) {
    static const char* keys[1] = {"msg"};
    const char* vals_arr[1];
    vals_arr[0] = msg ? msg : "unknown";
    /* Casting away const for simplicity; in real runtime we'd copy or enforce const. */
    return drift_error_new((DriftStr*)keys, (DriftStr*)vals_arr, 1, "Error", "main", NULL, NULL, NULL, NULL, 0, NULL, NULL, NULL, 0);
}

struct Error* error_push_frame(struct Error* err, DriftStr module, DriftStr file, DriftStr func, int64_t line, DriftStr* cap_keys, DriftStr* cap_values, size_t cap_count) {
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

    /* Always extend the cap_counts array to match frame_count, even if cap_count == 0. */
    size_t* new_cap_counts = (size_t*)realloc(err->cap_counts, new_count * sizeof(size_t));
    if (new_cap_counts) {
        err->cap_counts = new_cap_counts;
        err->cap_counts[new_count - 1] = cap_count;
    }

    if (cap_count > 0 && cap_keys && cap_values) {
        size_t new_total = err->cap_total + cap_count;
        DriftStr* new_cap_keys = (DriftStr*)realloc(err->cap_keys, new_total * sizeof(DriftStr));
        DriftStr* new_cap_vals = (DriftStr*)realloc(err->cap_values, new_total * sizeof(DriftStr));
        if (new_cap_keys && new_cap_vals && err->cap_counts) {
            for (size_t i = 0; i < cap_count; i++) {
                new_cap_keys[err->cap_total + i] = cap_keys[i];
                new_cap_vals[err->cap_total + i] = cap_values[i];
            }
            err->cap_keys = new_cap_keys;
            err->cap_values = new_cap_vals;
            err->cap_total = new_total;
        } else {
            free(new_cap_keys);
            free(new_cap_vals);
        }
    }
    return err;
}
