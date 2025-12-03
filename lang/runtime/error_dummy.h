// Minimal dummy error constructor for SSA error-path testing.
#pragma once

#include <stdint.h>
#include <stddef.h>
#include "string_runtime.h"

#define DRIFT_EVENT_KIND_TEST 0
#define DRIFT_EVENT_PAYLOAD_MASK ((1ULL << 60) - 1)

struct DriftErrorArg {
    struct DriftString key;
    struct DriftString value;
};

struct DriftError {
    int64_t code;               // matches Drift Int (word-sized)
    struct DriftString payload; // legacy first payload field (if provided)
    struct DriftErrorArg* args; // dynamic array of args (key/value)
    size_t arg_count;           // number of entries in args
};

// Returns a non-null Error* for testing error-edge lowering.
struct DriftError* drift_error_new_dummy(int64_t code, struct DriftString key, struct DriftString payload);
int64_t drift_error_get_code(struct DriftError* err);
// Returns pointer to value if found, NULL otherwise. No ownership transfer.
const struct DriftString* drift_error_get_arg(const struct DriftError* err, const struct DriftString* key);
