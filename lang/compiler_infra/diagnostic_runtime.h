#pragma once

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

// Foundational scalar typedefs and the `DriftString` foundational
// substrate.  These are NOT DV-specific — they were historically
// hosted in this header alongside the legacy `DriftDiagnosticValue`
// substrate and stayed here when the DV machinery was deleted in
// Slice 7c-3.  Kept here (not relocated) to avoid churn in the
// large set of `#include "diagnostic_runtime.h"` callers; once a
// future slice creates a dedicated `drift_runtime_types.h` they can
// move.
typedef ptrdiff_t drift_isize;
typedef size_t drift_usize;

_Static_assert(sizeof(drift_isize) == sizeof(void*), "drift_isize must be pointer-sized");
_Static_assert(sizeof(drift_usize) == sizeof(void*), "drift_usize must be pointer-sized");

#ifndef DRIFT_STRING_RUNTIME_H
struct DriftString {
    drift_isize len;
    char* data;
};
#endif

// Slice 7c-3 (ABI 14, 2026-05-06): the `DriftDiagnosticValue` /
// `DriftDiagnosticEntry` / `DriftDiagnosticField` /
// `DriftDiagnosticArray` / `DriftDiagnosticObject` struct types
// and the `DriftDiagnosticTag` enum are deleted along with
// `TypeKind.DIAGNOSTICVALUE` and the rest of the compiler-internal
// DV substrate.  No `drift_dv_*` / `drift_diag_from_*` callable
// surface remained after Slice 7c-1; no runtime / stdlib / test
// trampoline references the struct types.

#ifdef __cplusplus
}
#endif
