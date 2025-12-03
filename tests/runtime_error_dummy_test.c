#include "lang/runtime/error_dummy.h"
#include <assert.h>

int main(void) {
    struct DriftString empty = {0, NULL};
    struct DriftError* err = drift_error_new_dummy((int64_t)0xFFFFFFFFFFFFFFFFULL, empty, empty);
    uint64_t code = (uint64_t)err->code;
    uint64_t kind = code >> 60;
    uint64_t payload = code & DRIFT_EVENT_PAYLOAD_MASK;
    assert(kind == DRIFT_EVENT_KIND_TEST);
    assert(payload == (0xFFFFFFFFFFFFFFFFULL & DRIFT_EVENT_PAYLOAD_MASK));
    return 0;
}
