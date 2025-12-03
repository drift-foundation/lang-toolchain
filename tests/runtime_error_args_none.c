#include "lang/runtime/error_dummy.h"
#include <assert.h>

int main(void) {
    struct DriftString empty = {0, NULL};
    struct DriftError* err = drift_error_new_dummy(0, empty, empty);
    struct DriftString key = drift_string_from_cstr("missing");
    struct DriftOptionString opt = __exc_args_get(err, key);
    assert(opt.is_some == 0);
    return 0;
}
