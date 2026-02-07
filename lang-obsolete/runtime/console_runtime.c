// Drift console runtime (SSA backend)
#include "console_runtime.h"
#include <stdlib.h>

void drift_console_write(struct DriftString s) {
    if (s.len == 0 || s.data == NULL) {
        return;
    }
    size_t n = (size_t)s.len;
    size_t written = fwrite(s.data, 1, n, stdout);
    if (written < n && ferror(stdout)) {
        abort();
    }
}

void drift_console_writeln(struct DriftString s) {
    drift_console_write(s);
    fputc('\n', stdout);
}
