#include "string_runtime.h"

#include <stdlib.h>
#include <string.h>

struct DriftString drift_string_from_cstr(const char* cstr) {
    if (cstr == NULL) {
        struct DriftString s = {0, NULL};
        return s;
    }
    size_t len = strlen(cstr);
    char* buf = (char*)malloc(len + 1);
    if (!buf) {
        abort();
    }
    if (buf) {
        memcpy(buf, cstr, len);
        buf[len] = '\0';
    }
    struct DriftString s = {(uintptr_t)len, buf};
    return s;
}

struct DriftString drift_string_from_bytes(const char* data, uintptr_t len) {
    if (data == NULL || len == 0) {
        struct DriftString s = {0, NULL};
        return s;
    }
    char* buf = (char*)malloc((size_t)len + 1);
    if (!buf) {
        abort();
    }
    if (buf) {
        memcpy(buf, data, (size_t)len);
        buf[len] = '\0';
    }
    struct DriftString s = {len, buf};
    return s;
}

struct DriftString drift_string_literal(const char* data, uintptr_t len) {
    struct DriftString s = {len, (char*)data};
    return s;
}

struct DriftString drift_string_concat(struct DriftString a, struct DriftString b) {
    uintptr_t total = a.len + b.len;
    if (total == 0) {
        struct DriftString s = {0, NULL};
        return s;
    }
    char* buf = (char*)malloc((size_t)total + 1);
    if (!buf) {
        abort();
    }
    if (buf) {
        if (a.len > 0 && a.data) {
            memcpy(buf, a.data, (size_t)a.len);
        }
        if (b.len > 0 && b.data) {
            memcpy(buf + a.len, b.data, (size_t)b.len);
        }
        buf[total] = '\0';
    }
    struct DriftString s = {total, buf};
    return s;
}

void drift_string_free(struct DriftString s) {
    if (s.data) {
        free(s.data);
    }
}

char* drift_string_to_cstr(struct DriftString s) {
    size_t len = (size_t)s.len;
    char* buf = (char*)malloc(len + 1);
    if (!buf) {
        return NULL;
    }
    if (s.data && s.len > 0) {
        memcpy(buf, s.data, len);
    }
    buf[len] = '\0';
    return buf;
}
