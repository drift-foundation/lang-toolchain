#pragma once

#include <stdint.h>
#include "diagnostic_runtime.h"

#ifdef __cplusplus
extern "C" {
#endif

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
    int64_t code;
    struct DriftErrorAttr* attrs; // typed attrs (key -> DiagnosticValue)
    size_t attr_count;
    struct DriftCtxFrame* frames; // captured locals/frames (not yet used)
    size_t frame_count;
};

struct DriftError* drift_error_new_dummy(int64_t code, struct DriftString key, struct DriftString payload);
void drift_error_add_attr_dv(struct DriftError* err, struct DriftString key, const struct DriftDiagnosticValue* value);
void drift_error_add_local_dv(struct DriftError* err, struct DriftString frame, struct DriftString key, struct DriftDiagnosticValue value);
int64_t drift_error_get_code(struct DriftError* err);
const struct DriftDiagnosticValue* drift_error_get_attr(const struct DriftError* err, const struct DriftString* key);

// Typed attrs accessors used by lowered code.
struct DriftOptionalString __exc_attrs_get(const struct DriftError* err, struct DriftString key);
void __exc_attrs_get_dv(struct DriftDiagnosticValue* out, const struct DriftError* err, struct DriftString key);
struct DriftError* drift_error_new_with_payload(int64_t code, struct DriftString key, struct DriftDiagnosticValue payload);

#ifdef __cplusplus
}
#endif
