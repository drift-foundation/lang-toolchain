#pragma once
#include <stdint.h>
#include <stddef.h>
#include "string_runtime.h"

struct Error {
    struct DriftString event;
    struct DriftString domain;
    struct DriftString* keys;
    struct DriftString* values;
    size_t attr_count;
    struct DriftString* frame_modules;
    struct DriftString* frame_files;
    struct DriftString* frame_funcs;
    size_t* frame_lines;
    size_t frame_count;
    struct DriftString* cap_keys;
    struct DriftString* cap_values;
    size_t* cap_counts; /* per-frame counts, length = frame_count */
    size_t cap_total;
    char* diag;
};

struct Pair {
    int64_t val;
    struct Error* err;
};

struct Error* error_new(const char* msg); /* legacy helper for tests */
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
    size_t cap_total);
struct Error* error_push_frame(struct Error* err, struct DriftString module, struct DriftString file, struct DriftString func, int64_t line, struct DriftString* cap_keys, struct DriftString* cap_values, size_t cap_count);
const char* error_to_cstr(struct Error*);
void error_free(struct Error*);
