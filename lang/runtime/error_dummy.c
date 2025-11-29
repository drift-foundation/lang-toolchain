// Minimal dummy error constructor for SSA error-path testing.
#include "error_dummy.h"

static struct DriftError DUMMY_ERR = {1};

struct DriftError* drift_error_new_dummy(int code) {
    (void)code;
    return &DUMMY_ERR;
}

