#include <stdint.h>

int32_t takes_two_ptrs(uint8_t *a, uint8_t *b) {
    if (a && b) return 2;
    if (a) return 1;
    return 0;
}
