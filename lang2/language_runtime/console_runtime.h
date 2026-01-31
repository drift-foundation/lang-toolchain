// Drift console runtime (lang2, v1).
#pragma once

#include "string_runtime.h"

#ifdef __cplusplus
extern "C" {
#endif

void drift_console_write(DriftString s);
void drift_console_writeln(DriftString s);
void drift_console_eprintln(DriftString s);

#ifdef __cplusplus
}
#endif
