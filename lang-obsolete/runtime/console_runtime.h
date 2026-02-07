// Drift console runtime (SSA backend)
#pragma once

#include <stdio.h>
#include "string_runtime.h"

#ifdef __cplusplus
extern "C" {
#endif

// Write a DriftString to stdout (len bytes, no extra NUL).
void drift_console_write(struct DriftString s);
// Write a DriftString followed by '\n' to stdout.
void drift_console_writeln(struct DriftString s);

#ifdef __cplusplus
}
#endif
