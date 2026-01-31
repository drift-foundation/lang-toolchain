#ifndef LANG2_ARGV_RUNTIME_H
#define LANG2_ARGV_RUNTIME_H

#include "array_runtime.h"
#include "string_runtime.h"

typedef DriftArrayHeader DriftArrayString;

// Build an Array<String> header from argc/argv using existing string/array runtimes.
// The result is written into `out` to avoid ABI surprises with large structs.
void drift_build_argv(DriftArrayString *out, int argc, char **argv);

#endif // LANG2_ARGV_RUNTIME_H
