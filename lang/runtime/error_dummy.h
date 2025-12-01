// Minimal dummy error constructor for SSA error-path testing.
#pragma once

#include <stdint.h>
#include "string_runtime.h"

struct DriftError {
    int64_t code;               // matches Drift Int (word-sized)
    struct DriftString payload; // first payload field (if provided)
};

// Returns a non-null Error* for testing error-edge lowering.
struct DriftError* drift_error_new_dummy(int64_t code, struct DriftString payload);
int64_t drift_error_get_code(struct DriftError* err);
