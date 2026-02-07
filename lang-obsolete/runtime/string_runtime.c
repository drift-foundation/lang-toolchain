// Drift String runtime support (SSA backend)
#include "string_runtime.h"

#include <stdlib.h>
#include <string.h>
#include <stdio.h>

struct DriftString drift_string_from_cstr(const char* cstr) {
    if (cstr == NULL) {
        struct DriftString s = {0, NULL};
        return s;
    }
    drift_size_t len = (drift_size_t)strlen(cstr);
    char* buf = (char*)malloc((size_t)len + 1);
    if (!buf) {
        abort();
    }
    memcpy(buf, cstr, (size_t)len);
    buf[len] = '\0';
    struct DriftString s = {len, buf};
    return s;
}

struct DriftString drift_string_from_utf8_bytes(const char* data, drift_size_t len) {
    if (data == NULL || len == 0) {
        struct DriftString s = {0, NULL};
        return s;
    }
    char* buf = (char*)malloc((size_t)len + 1);
    if (!buf) {
        abort();
    }
    memcpy(buf, data, (size_t)len);
    buf[len] = '\0';
    struct DriftString s = {len, buf};
    return s;
}

struct DriftString drift_string_from_int64(int64_t v) {
    /* worst-case length for int64_t in decimal, including sign */
    char buf[32];
    int n = snprintf(buf, sizeof(buf), "%lld", (long long)v);
    if (n < 0) {
        abort();
    }
    return drift_string_from_utf8_bytes(buf, (drift_size_t)n);
}

struct DriftString drift_string_from_bool(int v) {
    if (v) {
        return drift_string_literal("true", 4);
    } else {
        return drift_string_literal("false", 5);
    }
}

struct DriftString drift_string_literal(const char* data, drift_size_t len) {
    struct DriftString s = {len, (char*)data};
    return s;
}

struct DriftString drift_string_concat(struct DriftString a, struct DriftString b) {
    if ((size_t)-1 - (size_t)a.len < (size_t)b.len) {
        abort();
    }
    drift_size_t total = a.len + b.len;
    if (total == 0) {
        struct DriftString s = {0, NULL};
        return s;
    }
    char* buf = (char*)malloc((size_t)total + 1);
    if (!buf) {
        abort();
    }
    if (a.len > 0 && a.data) {
        memcpy(buf, a.data, (size_t)a.len);
    }
    if (b.len > 0 && b.data) {
        memcpy(buf + a.len, b.data, (size_t)b.len);
    }
    buf[total] = '\0';
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
        abort();
    }
    if (s.data && s.len > 0) {
        memcpy(buf, s.data, len);
    }
    buf[len] = '\0';
    return buf;
}

int drift_string_eq(struct DriftString a, struct DriftString b) {
    if (a.len != b.len) {
        return 0;
    }
    if (a.len == 0) {
        return 1;
    }
    if (!a.data || !b.data) {
        return 0;
    }
    return memcmp(a.data, b.data, (size_t)a.len) == 0;
}
