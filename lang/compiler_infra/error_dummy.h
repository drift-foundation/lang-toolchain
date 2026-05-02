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
    // ── Legacy DV path (kept fully functional during the DV→JSON
    // migration; deleted at Phase 2 / ABI 12).
    struct DriftErrorAttr* attrs; // typed attrs (key -> DiagnosticValue)
    size_t attr_count;
    struct DriftCtxFrame* frames; // captured locals/frames (not yet used)
    size_t frame_count;
    // ── New JSON path (Phase 1 additive; sole survivor at ABI 12).
    // `params_json` is a single canonical JSON object document holding
    // declared exception field values; `context_json` is a single
    // canonical JSON array document holding `^`-captured frame objects.
    // Empty form: `params_json == "{}"`, `context_json == "[]"`.  The
    // runtime owns both buffers; getter helpers return retained copies.
    // See ABI spec §2.3 for the helper ownership contract.
    struct DriftString params_json;
    struct DriftString context_json;
};

struct DriftError* drift_error_new_dummy(drift_error_code_t code, struct DriftString event_fqn, struct DriftString key, struct DriftString payload);
struct DriftError* drift_error_new(drift_error_code_t code, struct DriftString event_fqn);
void drift_error_add_attr_dv(struct DriftError* err, struct DriftString key, const struct DriftDiagnosticValue* value);
void drift_error_add_local_dv(struct DriftError* err, struct DriftString frame, struct DriftString key, const struct DriftDiagnosticValue* value);
drift_error_code_t drift_error_get_code(struct DriftError* err);
struct DriftString drift_error_get_event_fqn(const struct DriftError* err);
const struct DriftDiagnosticValue* drift_error_get_attr(const struct DriftError* err, const struct DriftString* key);

// Typed attrs accessors used by lowered code.
uint8_t __exc_attrs_get(struct DriftString* out, const struct DriftError* err, struct DriftString key);
void __exc_attrs_get_dv(struct DriftDiagnosticValue* out, const struct DriftError* err, struct DriftString key);
void __exc_captures_get_dv(struct DriftDiagnosticValue* out, const struct DriftError* err, struct DriftString frame, struct DriftString key);
struct DriftError* drift_error_new_with_payload(drift_error_code_t code, struct DriftString event_fqn, struct DriftString key, const struct DriftDiagnosticValue* payload);
void drift_error_release(struct DriftError* err);
void drift_error_raise(struct DriftError* err) __attribute__((noreturn));

// ── Phase 1 additive JSON helpers (ABI 11; permanent at ABI 12).
//
// Ownership contract (see drift-lang-abi.md §2.3):
//   - `set_params_json` takes ownership of `params_json`; on
//     replacement releases the prior value exactly once.
//   - `append_context_frame` takes ownership of `frame_json`; the
//     runtime rebuilds an owned `context_json` with the frame appended.
//     Bytes of `frame_json` are preserved verbatim inside the merged
//     array (ABI §2.2 fastpath guarantee).
//   - getters return RETAINED `DriftString` (caller releases); safe
//     to surface as a normal Drift `String` return.
void drift_error_set_params_json(struct DriftError* err, struct DriftString params_json);
void drift_error_append_context_frame(struct DriftError* err, struct DriftString frame_json);
struct DriftString drift_error_get_params_json(const struct DriftError* err);
struct DriftString drift_error_get_context_json(const struct DriftError* err);

#ifdef __cplusplus
}
#endif
