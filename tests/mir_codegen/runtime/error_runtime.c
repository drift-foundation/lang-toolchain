#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include "error_runtime.h"
#include "string_runtime.h"

static struct DriftString clone_string(struct DriftString s) {
    if (s.len == 0 || s.data == NULL) {
        return drift_string_empty();
    }
    return drift_string_from_utf8_bytes(s.data, s.len);
}

static struct DriftString make_literal(const char* s) {
    if (!s) {
        return drift_string_empty();
    }
    size_t len = strlen(s);
    return drift_string_literal(s, len);
}

static void free_string_array(struct DriftString* arr, size_t count) {
    if (!arr) return;
    for (size_t i = 0; i < count; i++) {
        drift_string_free(arr[i]);
    }
    free(arr);
}

static struct DriftString* clone_string_array(struct DriftString* src, size_t count) {
    if (!src || count == 0) return NULL;
    struct DriftString* dst = (struct DriftString*)malloc(count * sizeof(struct DriftString));
    if (!dst) return NULL;
    for (size_t i = 0; i < count; i++) {
        dst[i] = clone_string(src[i]);
    }
    return dst;
}

struct Error* drift_error_new(
    struct DriftString* keys,
    struct DriftString* values,
    size_t attr_count,
    struct DriftString event,
    struct DriftString domain,
    struct DriftString* frame_modules,
    struct DriftString* frame_files,
    struct DriftString* frame_funcs,
    size_t* frame_lines,
    size_t frame_count,
    struct DriftString* cap_keys,
    struct DriftString* cap_values,
    size_t* cap_counts,
    size_t cap_total) {
    struct Error* err = (struct Error*)calloc(1, sizeof(struct Error));
    if (!err) return NULL;
    err->event = clone_string(event.len ? event : make_literal("unknown"));
    err->domain = clone_string(domain.len ? domain : make_literal("main"));
    err->attr_count = attr_count;
    if (attr_count > 0 && keys && values) {
        err->keys = clone_string_array(keys, attr_count);
        err->values = clone_string_array(values, attr_count);
        if (!err->keys || !err->values) {
            free_string_array(err->keys, attr_count);
            free_string_array(err->values, attr_count);
            drift_string_free(err->event);
            drift_string_free(err->domain);
            free(err);
            return NULL;
        }
    }
    err->frame_count = frame_count;
    if (frame_count > 0 && frame_modules && frame_files && frame_funcs && frame_lines) {
        err->frame_modules = clone_string_array(frame_modules, frame_count);
        err->frame_files = clone_string_array(frame_files, frame_count);
        err->frame_funcs = clone_string_array(frame_funcs, frame_count);
        err->frame_lines = (size_t*)malloc(frame_count * sizeof(size_t));
        if (!err->frame_modules || !err->frame_files || !err->frame_funcs || !err->frame_lines) {
            free_string_array(err->frame_modules, frame_count);
            free_string_array(err->frame_files, frame_count);
            free_string_array(err->frame_funcs, frame_count);
            free(err->frame_lines);
            free_string_array(err->keys, err->attr_count);
            free_string_array(err->values, err->attr_count);
            drift_string_free(err->event);
            drift_string_free(err->domain);
            free(err);
            return NULL;
        }
        for (size_t i = 0; i < frame_count; i++) {
            err->frame_lines[i] = frame_lines[i];
        }
    }
    err->cap_counts = NULL;
    err->cap_keys = NULL;
    err->cap_values = NULL;
    err->cap_total = 0;
    if (frame_count > 0) {
        err->cap_counts = (size_t*)calloc(frame_count, sizeof(size_t));
        if (!err->cap_counts) {
            free_string_array(err->frame_modules, frame_count);
            free_string_array(err->frame_files, frame_count);
            free_string_array(err->frame_funcs, frame_count);
            free(err->frame_lines);
            free_string_array(err->keys, err->attr_count);
            free_string_array(err->values, err->attr_count);
            drift_string_free(err->event);
            drift_string_free(err->domain);
            free(err);
            return NULL;
        }
        if (cap_total > 0 && cap_keys && cap_values && cap_counts) {
            err->cap_keys = (struct DriftString*)malloc(cap_total * sizeof(struct DriftString));
            err->cap_values = (struct DriftString*)malloc(cap_total * sizeof(struct DriftString));
            if (!err->cap_keys || !err->cap_values) {
                free(err->cap_keys);
                free(err->cap_values);
                free(err->cap_counts);
                free_string_array(err->frame_modules, frame_count);
                free_string_array(err->frame_files, frame_count);
                free_string_array(err->frame_funcs, frame_count);
                free(err->frame_lines);
                free_string_array(err->keys, err->attr_count);
                free_string_array(err->values, err->attr_count);
                drift_string_free(err->event);
                drift_string_free(err->domain);
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
                        err->cap_keys[idx] = clone_string(cap_keys[idx]);
                        err->cap_values[idx] = clone_string(cap_values[idx]);
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

    const char* event = (err->event.data && err->event.len) ? err->event.data : "unknown";
    const char* domain = (err->domain.data && err->domain.len) ? err->domain.data : "main";

    /* Build attrs JSON fragment. */
    size_t attrs_len = 0;
    for (size_t i = 0; i < err->attr_count; i++) {
        const char* k = (err->keys && err->keys[i].data) ? err->keys[i].data : "unknown";
        const char* v = (err->values && err->values[i].data) ? err->values[i].data : "unknown";
        attrs_len += (size_t)snprintf(NULL, 0, "\"%s\":\"%s\"", k, v);
        if (i + 1 < err->attr_count) attrs_len += 1; /* comma */
    }

    /* Build frames JSON fragment. */
    size_t frames_len = 0;
    size_t cap_offset = 0;
    for (size_t i = 0; i < err->frame_count; i++) {
        const char* module = (err->frame_modules && err->frame_modules[i].data) ? err->frame_modules[i].data : "<unknown>";
        const char* file = (err->frame_files && err->frame_files[i].data) ? err->frame_files[i].data : "<unknown>";
        const char* func = (err->frame_funcs && err->frame_funcs[i].data) ? err->frame_funcs[i].data : "<unknown>";
        unsigned long long line = (err->frame_lines) ? (unsigned long long)err->frame_lines[i] : 0;
        size_t cap_count = (err->cap_counts && i < err->frame_count) ? err->cap_counts[i] : 0;
        /* Build captured fragment inline. */
        size_t cap_len = 0;
        for (size_t j = 0; j < cap_count; j++) {
            const char* ck = (err->cap_keys && cap_offset + j < err->cap_total && err->cap_keys[cap_offset + j].data) ? err->cap_keys[cap_offset + j].data : "unknown";
            const char* cv = (err->cap_values && cap_offset + j < err->cap_total && err->cap_values[cap_offset + j].data) ? err->cap_values[cap_offset + j].data : "unknown";
            cap_len += (size_t)snprintf(NULL, 0, "\"%s\":\"%s\"", ck, cv);
            if (j + 1 < cap_count) cap_len += 1;
        }
        char* cap_buf = (char*)malloc(cap_len + 1);
        if (!cap_buf && cap_len > 0) { return NULL; }
        if (cap_len > 0) {
            char* cp = cap_buf;
            for (size_t j = 0; j < cap_count; j++) {
                const char* ck = (err->cap_keys && cap_offset + j < err->cap_total && err->cap_keys[cap_offset + j].data) ? err->cap_keys[cap_offset + j].data : "unknown";
                const char* cv = (err->cap_values && cap_offset + j < err->cap_total && err->cap_values[cap_offset + j].data) ? err->cap_values[cap_offset + j].data : "unknown";
                cp += snprintf(cp, cap_len - (size_t)(cp - cap_buf) + 1, "\"%s\":\"%s\"", ck, cv);
                if (j + 1 < cap_count) *cp++ = ',';
            }
            *cp = '\0';
        } else if (cap_buf) {
            cap_buf[0] = '\0';
        }
        frames_len += (size_t)snprintf(NULL, 0, "{\"module\":\"%s\",\"file\":\"%s\",\"func\":\"%s\",\"line\":%llu,\"captured\":{%s}}", module, file, func, line, cap_len > 0 ? cap_buf : "");
        free(cap_buf);
        cap_offset += cap_count;
        if (i + 1 < err->frame_count) frames_len += 1; /* comma */
    }

    char* attrs_buf = NULL;
    if (attrs_len > 0) {
        attrs_buf = (char*)malloc(attrs_len + 1);
        if (!attrs_buf) return NULL;
        char* p = attrs_buf;
        for (size_t i = 0; i < err->attr_count; i++) {
            const char* k = (err->keys && err->keys[i].data) ? err->keys[i].data : "unknown";
            const char* v = (err->values && err->values[i].data) ? err->values[i].data : "unknown";
            p += snprintf(p, attrs_len - (size_t)(p - attrs_buf) + 1, "\"%s\":\"%s\"", k, v);
            if (i + 1 < err->attr_count) *p++ = ',';
        }
        *p = '\0';
    }

    char* frames_buf = NULL;
    if (frames_len > 0) {
        frames_buf = (char*)malloc(frames_len + 1);
        if (!frames_buf) { free(attrs_buf); return NULL; }
        char* p = frames_buf;
        size_t cap_offset2 = 0;
        for (size_t i = 0; i < err->frame_count; i++) {
            const char* module = (err->frame_modules && err->frame_modules[i].data) ? err->frame_modules[i].data : "<unknown>";
            const char* file = (err->frame_files && err->frame_files[i].data) ? err->frame_files[i].data : "<unknown>";
            const char* func = (err->frame_funcs && err->frame_funcs[i].data) ? err->frame_funcs[i].data : "<unknown>";
            unsigned long long line = (err->frame_lines) ? (unsigned long long)err->frame_lines[i] : 0;
            size_t cap_count = (err->cap_counts && i < err->frame_count) ? err->cap_counts[i] : 0;
            /* Build captured fragment inline. */
            size_t cap_len = 0;
            for (size_t j = 0; j < cap_count; j++) {
                const char* ck = (err->cap_keys && cap_offset2 + j < err->cap_total && err->cap_keys[cap_offset2 + j].data) ? err->cap_keys[cap_offset2 + j].data : "unknown";
                const char* cv = (err->cap_values && cap_offset2 + j < err->cap_total && err->cap_values[cap_offset2 + j].data) ? err->cap_values[cap_offset2 + j].data : "unknown";
                cap_len += (size_t)snprintf(NULL, 0, "\"%s\":\"%s\"", ck, cv);
                if (j + 1 < cap_count) cap_len += 1;
            }
            char* cap_buf = (char*)malloc(cap_len + 1);
            if (!cap_buf && cap_len > 0) { free(attrs_buf); free(frames_buf); return NULL; }
            if (cap_len > 0) {
                char* cp = cap_buf;
                for (size_t j = 0; j < cap_count; j++) {
                    const char* ck = (err->cap_keys && cap_offset2 + j < err->cap_total && err->cap_keys[cap_offset2 + j].data) ? err->cap_keys[cap_offset2 + j].data : "unknown";
                    const char* cv = (err->cap_values && cap_offset2 + j < err->cap_total && err->cap_values[cap_offset2 + j].data) ? err->cap_values[cap_offset2 + j].data : "unknown";
                    cp += snprintf(cp, cap_len - (size_t)(cp - cap_buf) + 1, "\"%s\":\"%s\"", ck, cv);
                    if (j + 1 < cap_count) *cp++ = ',';
                }
                *cp = '\0';
            } else if (cap_buf) {
                cap_buf[0] = '\0';
            }
            p += snprintf(p, frames_len - (size_t)(p - frames_buf) + 1,
                "{\"module\":\"%s\",\"file\":\"%s\",\"func\":\"%s\",\"line\":%llu,\"captured\":{%s}}",
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
        free(attrs_section);
        free(frames_section);
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
    drift_string_free(err->event);
    drift_string_free(err->domain);
    free_string_array(err->keys, err->attr_count);
    free_string_array(err->values, err->attr_count);
    free_string_array(err->frame_modules, err->frame_count);
    free_string_array(err->frame_files, err->frame_count);
    free_string_array(err->frame_funcs, err->frame_count);
    free(err->frame_lines);
    free_string_array(err->cap_keys, err->cap_total);
    free_string_array(err->cap_values, err->cap_total);
    free(err->cap_counts);
    free(err);
}

struct Error* error_new(const char* msg) {
    struct DriftString key = drift_string_from_cstr("msg");
    struct DriftString val = msg ? drift_string_from_cstr(msg) : drift_string_from_cstr("unknown");
    struct DriftString keys[1] = {key};
    struct DriftString vals_arr[1] = {val};
    struct DriftString ev = drift_string_from_cstr("Error");
    struct DriftString dom = drift_string_from_cstr("main");
    struct Error* err = drift_error_new(keys, vals_arr, 1, ev, dom, NULL, NULL, NULL, NULL, 0, NULL, NULL, NULL, 0);
    drift_string_free(key);
    drift_string_free(val);
    drift_string_free(ev);
    drift_string_free(dom);
    return err;
}

struct Error* error_push_frame(struct Error* err, struct DriftString module, struct DriftString file, struct DriftString func, int64_t line, struct DriftString* cap_keys, struct DriftString* cap_values, size_t cap_count) {
    if (!err) return NULL;
    size_t new_count = err->frame_count + 1;
    struct DriftString* new_modules = (struct DriftString*)realloc(err->frame_modules, new_count * sizeof(struct DriftString));
    struct DriftString* new_files = (struct DriftString*)realloc(err->frame_files, new_count * sizeof(struct DriftString));
    struct DriftString* new_funcs = (struct DriftString*)realloc(err->frame_funcs, new_count * sizeof(struct DriftString));
    size_t* new_lines = (size_t*)realloc(err->frame_lines, new_count * sizeof(size_t));
    if (!new_modules || !new_files || !new_funcs || !new_lines) {
        /* Best effort: free any new allocations and leave the error unchanged. */
        free(new_modules);
        free(new_files);
        free(new_funcs);
        free(new_lines);
        return err;
    }
    err->frame_modules = new_modules;
    err->frame_files = new_files;
    err->frame_funcs = new_funcs;
    err->frame_lines = new_lines;
    err->frame_modules[err->frame_count] = clone_string(module.len ? module : make_literal("<unknown>"));
    err->frame_files[err->frame_count] = clone_string(file.len ? file : make_literal("<unknown>"));
    err->frame_funcs[err->frame_count] = clone_string(func.len ? func : make_literal("<unknown>"));
    err->frame_lines[err->frame_count] = (size_t)line;
    err->frame_count = new_count;

    /* Always extend the cap_counts array to match frame_count, even if cap_count == 0. */
    size_t* new_cap_counts = (size_t*)realloc(err->cap_counts, new_count * sizeof(size_t));
    if (new_cap_counts) {
        err->cap_counts = new_cap_counts;
        err->cap_counts[new_count - 1] = cap_count;
    }

    if (cap_count > 0 && cap_keys && cap_values) {
        size_t new_total = err->cap_total + cap_count;
        struct DriftString* new_cap_keys = (struct DriftString*)realloc(err->cap_keys, new_total * sizeof(struct DriftString));
        struct DriftString* new_cap_vals = (struct DriftString*)realloc(err->cap_values, new_total * sizeof(struct DriftString));
        if (new_cap_keys && new_cap_vals && err->cap_counts) {
            for (size_t i = 0; i < cap_count; i++) {
                new_cap_keys[err->cap_total + i] = clone_string(cap_keys[i]);
                new_cap_vals[err->cap_total + i] = clone_string(cap_values[i]);
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
