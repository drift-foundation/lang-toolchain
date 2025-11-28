#pragma once

#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>

#ifdef __cplusplus
extern "C" {
#endif

// Drift size carrier for ABI (must match Drift Size).
typedef uintptr_t drift_size_t;

// Simple heap-allocated UTF-8 string representation.
// Ownership: callers own DriftString values and must call drift_string_free
// when they no longer need the heap buffer (except for literals that point
// to static storage).
struct DriftString {
    drift_size_t len;  // number of bytes (UTF-8)
    char* data;        // pointer to UTF-8 bytes (may be NULL if len == 0); heap buffers include a trailing NUL
};

// Construct from a null-terminated UTF-8 C string (copies data).
struct DriftString drift_string_from_cstr(const char* cstr);

// Construct from UTF-8 bytes (copies data).
struct DriftString drift_string_from_utf8_bytes(const char* data, drift_size_t len);
struct DriftString drift_string_from_int64(int64_t v);
struct DriftString drift_string_from_bool(int v);

// Construct a DriftString backed by static storage (no free needed; MUST NOT be passed to drift_string_free).
struct DriftString drift_string_literal(const char* data, drift_size_t len);

// Concatenate two strings (allocates a new buffer).
struct DriftString drift_string_concat(struct DriftString a, struct DriftString b);

// Free the heap buffer of a DriftString (safe to call on zeroed structs).
void drift_string_free(struct DriftString s);

// Convert to a null-terminated C string; caller owns the returned buffer.
char* drift_string_to_cstr(struct DriftString s);

// Accessors for length and data pointer.
static inline uintptr_t drift_string_len(struct DriftString s) { return s.len; }
static inline const char* drift_string_ptr(struct DriftString s) { return s.data; }

// Empty string helper.
static inline struct DriftString drift_string_empty(void) {
    struct DriftString s;
    s.len = 0;
    s.data = (char*)malloc(1);
    if (!s.data) {
        abort();
    }
    s.data[0] = '\0';
    return s;
}

#ifdef __cplusplus
}
#endif
