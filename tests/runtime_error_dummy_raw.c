#include "runtime/error_dummy.h"
#include <assert.h>

static void check(uint64_t raw_in) {
    struct DriftString empty = (struct DriftString){0, NULL};
    struct DriftError* err = drift_error_new_dummy((int64_t)raw_in, empty, empty);
    uint64_t raw = (uint64_t)err->code;
    uint64_t kind = raw >> 60;
    uint64_t payload = raw & DRIFT_EVENT_PAYLOAD_MASK;
    assert(kind == (raw_in >> 60));
    assert(payload == (raw_in & DRIFT_EVENT_PAYLOAD_MASK));
}

int main(void) {
    check(0ull);
    check(0x1ull);                       // small payload
    check(0xF000000000000000ull);        // kind bits only
    check(0x1234567890ABCDEFull);        // mid-range
    check(0xFFFFFFFFFFFFFFFFull);        // all ones
    return 0;
}
