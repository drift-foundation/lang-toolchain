#pragma once

#include <stdint.h>
#include "diagnostic_runtime.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef uint64_t drift_error_code_t;

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
    drift_error_code_t code;
    // Canonical fully-qualified exception label (module.sub:Event). This is
    // a debug/logging aid; routing/matching is always by `code`.
    struct DriftString event_fqn;
    struct DriftErrorAttr* attrs; // typed attrs (key -> DiagnosticValue)
    size_t attr_count;
    struct DriftCtxFrame* frames; // captured locals/frames (not yet used)
    size_t frame_count;
};

struct DriftError* drift_error_new_dummy(drift_error_code_t code, struct DriftString event_fqn, struct DriftString key, struct DriftString payload);
struct DriftError* drift_error_new(drift_error_code_t code, struct DriftString event_fqn);
void drift_error_add_attr_dv(struct DriftError* err, struct DriftString key, const struct DriftDiagnosticValue* value);
void drift_error_add_local_dv(struct DriftError* err, struct DriftString frame, struct DriftString key, struct DriftDiagnosticValue value);
drift_error_code_t drift_error_get_code(struct DriftError* err);
struct DriftString drift_error_get_event_fqn(const struct DriftError* err);
const struct DriftDiagnosticValue* drift_error_get_attr(const struct DriftError* err, const struct DriftString* key);

// Typed attrs accessors used by lowered code.
uint8_t __exc_attrs_get(struct DriftString* out, const struct DriftError* err, struct DriftString key);
void __exc_attrs_get_dv(struct DriftDiagnosticValue* out, const struct DriftError* err, struct DriftString key);
struct DriftError* drift_error_new_with_payload(drift_error_code_t code, struct DriftString event_fqn, struct DriftString key, const struct DriftDiagnosticValue* payload);
void drift_error_raise(struct DriftError* err) __attribute__((noreturn));

#ifdef __cplusplus
}
#endif
