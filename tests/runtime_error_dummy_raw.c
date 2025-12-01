#include "runtime/error_dummy.h"
#include <assert.h>

int main(void) {
    struct DriftString empty = {0, NULL};
    struct DriftError* err = drift_error_new_dummy((int64_t)0xFFFFFFFFFFFFFFFFULL, empty);
    uint64_t raw = (uint64_t)err->code;
    uint64_t kind = raw >> 60;
    uint64_t payload = raw & DRIFT_EVENT_PAYLOAD_MASK;
    assert(kind == 0xF); // upper bits preserved
    assert(payload == (0xFFFFFFFFFFFFFFFFULL & DRIFT_EVENT_PAYLOAD_MASK));
    return 0;
}
