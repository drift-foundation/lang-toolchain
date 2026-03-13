#include <stdlib.h>
#include <stdint.h>

uint8_t *alloc_byte(uint8_t b) {
    uint8_t *p = (uint8_t *)malloc(1);
    if (p) *p = b;
    return p;
}

void free_byte(uint8_t *ptr) {
    free(ptr);
}
