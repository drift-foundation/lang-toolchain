#ifndef DRIFT_RANDOM_RUNTIME_H
#define DRIFT_RANDOM_RUNTIME_H

#include <stdint.h>

/* Fill buf with len cryptographically secure random bytes via getrandom(2).
 * Returns 0 on success, or -errno on failure. Retries on EINTR. */
int64_t drift_random_fill(uint8_t *buf, int64_t len);

#endif  /* DRIFT_RANDOM_RUNTIME_H */
