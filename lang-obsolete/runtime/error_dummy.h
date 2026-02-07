// Minimal dummy error constructor for SSA error-path testing.
#pragma once

#include <stdint.h>
#include <stddef.h>
#include "string_runtime.h"
#include "diagnostic_runtime.h"

#define DRIFT_EVENT_KIND_TEST 0
#define DRIFT_EVENT_PAYLOAD_MASK ((1ULL << 60) - 1)

struct DriftErrorAttr {
    struct DriftString key;
    struct DriftDiagnosticValue value;
};

struct DriftErrorLocal {
    struct DriftString key;
    struct DriftDiagnosticValue value;
};

struct DriftCtxFrame {
    struct DriftString name;
    struct DriftErrorLocal* locals;
    size_t local_count;
};

struct DriftError {
    int64_t code;                // matches Drift Int (word-sized)
    struct DriftErrorAttr* attrs; // typed attrs (key -> DiagnosticValue)
    size_t attr_count;           // number of entries in attrs
    struct DriftCtxFrame* frames; // captured locals grouped by frame
    size_t frame_count;
};

_Static_assert(sizeof(struct DriftDiagnosticValue) == 24, "DriftDiagnosticValue size mismatch");
_Static_assert(_Alignof(struct DriftDiagnosticValue) == 8, "DriftDiagnosticValue alignment mismatch");

// Returns a non-null Error* for testing error-edge lowering.
struct DriftError* drift_error_new_dummy(int64_t code, struct DriftString key, struct DriftString payload);
int64_t drift_error_get_code(struct DriftError* err);
const struct DriftDiagnosticValue* drift_error_get_attr(const struct DriftError* err, const struct DriftString* key);
// Append an attr (key,value) to an existing error (typed).
void drift_error_add_attr_dv(struct DriftError* err, struct DriftString key, const struct DriftDiagnosticValue* value);
// Attach a captured local (typed DiagnosticValue) to a named frame.
void drift_error_add_local_dv(struct DriftError* err, struct DriftString frame, struct DriftString key, struct DriftDiagnosticValue value);
// Typed attr lookup (full DiagnosticValue). Writes Missing when absent.
void __exc_attrs_get_dv(struct DriftDiagnosticValue* out, const struct DriftError* err, struct DriftString key);

// Optional<Int> helpers for generic Optional coverage.
struct DriftOptionalInt drift_optional_int_some(int64_t value);
struct DriftOptionalInt drift_optional_int_none(void);
