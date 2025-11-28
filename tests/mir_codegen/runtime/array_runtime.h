#pragma once

#include <stddef.h>
#include <stdint.h>

#include "string_runtime.h"
#include "error_runtime.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Mirror Drift Size for array metadata. */
typedef drift_size_t drift_size;

/* Heap-backed array allocation used by codegen. */
void* drift_alloc_array(size_t elem_size, size_t elem_align, drift_size len, drift_size cap);

/* Bounds check failure: reports an IndexError and aborts. */
#if defined(__GNUC__) || defined(__clang__)
__attribute__((noreturn))
#endif
void drift_bounds_check_fail(drift_size idx, drift_size len);

#ifdef __cplusplus
}
#endif
