#include "random_runtime.h"

#include <errno.h>
#include <sys/random.h>

int64_t drift_random_fill(uint8_t *buf, int64_t len) {
	size_t remaining = (size_t)len;
	uint8_t *pos = buf;
	while (remaining > 0) {
		ssize_t n = getrandom(pos, remaining, 0);
		if (n < 0) {
			if (errno == EINTR) continue;
			return (int64_t)(-errno);
		}
		pos += n;
		remaining -= (size_t)n;
	}
	return 0;
}
