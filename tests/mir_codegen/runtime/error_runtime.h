#pragma once
#include <stdint.h>
#include <stddef.h>

typedef const char* DriftStr;

struct Error {
    DriftStr event;
    DriftStr domain;
    DriftStr* keys;
    DriftStr* values;
    size_t attr_count;
    DriftStr* frame_modules;
    DriftStr* frame_files;
    DriftStr* frame_funcs;
    int64_t* frame_lines;
    size_t frame_count;
    DriftStr* cap_keys;
    DriftStr* cap_values;
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
    size_t cap_total);
struct Error* error_push_frame(struct Error* err, DriftStr module, DriftStr file, DriftStr func, int64_t line, DriftStr* cap_keys, DriftStr* cap_values, size_t cap_count);
const char* error_to_cstr(struct Error*);
void error_free(struct Error*);
