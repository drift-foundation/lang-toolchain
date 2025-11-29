// Minimal dummy error constructor for SSA error-path testing.
// Returns a non-null Error* without pulling in the full error runtime.

struct DriftError {
    int code;
};

static struct DriftError DUMMY_ERR = {1};

struct DriftError* drift_error_new_dummy(int code) {
    (void)code;
    return &DUMMY_ERR;
}
