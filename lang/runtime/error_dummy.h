// Minimal dummy error constructor for SSA error-path testing.
#pragma once

struct DriftError {
    int code;
};

// Returns a non-null Error* for testing error-edge lowering.
struct DriftError* drift_error_new_dummy(int code);

