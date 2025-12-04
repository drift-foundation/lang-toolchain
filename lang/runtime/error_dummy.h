// Minimal dummy error constructor for SSA error-path testing.
#pragma once

#include <stdint.h>
#include <stddef.h>
#include "string_runtime.h"
#include "diagnostic_runtime.h"

#define DRIFT_EVENT_KIND_TEST 0
#define DRIFT_EVENT_PAYLOAD_MASK ((1ULL << 60) - 1)

struct DriftErrorArg {
    struct DriftString key;
    struct DriftString value;
};

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
    struct DriftString payload;  // legacy payload field (string)
    struct DriftErrorArg* args;  // legacy string args
    size_t arg_count;            // number of string args
    struct DriftErrorAttr* attrs; // typed attrs (key -> DiagnosticValue)
    size_t attr_count;           // number of entries in attrs
    struct DriftCtxFrame* frames; // captured locals grouped by frame
    size_t frame_count;
};

// Returns a non-null Error* for testing error-edge lowering.
struct DriftError* drift_error_new_dummy(int64_t code, struct DriftString key, struct DriftString payload);
int64_t drift_error_get_code(struct DriftError* err);
// Returns pointer to value if found, NULL otherwise. No ownership transfer.
const struct DriftString* drift_error_get_arg(const struct DriftError* err, const struct DriftString* key);
const struct DriftDiagnosticValue* drift_error_get_attr(const struct DriftError* err, const struct DriftString* key);
// Append an attr (key,value) to an existing error (value stored as DiagnosticValue::String).
void drift_error_add_arg(struct DriftError* err, struct DriftString key, struct DriftString value);
void drift_error_add_attr_dv(struct DriftError* err, struct DriftString key, const struct DriftDiagnosticValue* value);
// Attach a captured local (typed DiagnosticValue) to a named frame.
void drift_error_add_local_dv(struct DriftError* err, struct DriftString frame, struct DriftString key, struct DriftDiagnosticValue value);
// Typed attr lookup (full DiagnosticValue). Writes Missing when absent.
void __exc_attrs_get_dv(struct DriftDiagnosticValue* out, const struct DriftError* err, struct DriftString key);
// Optional<String> return for exception attr lookup (string-valued only).
struct DriftOptionalString __exc_args_get(const struct DriftError* err, struct DriftString key);
// Required attr lookup (string-valued only): returns empty string if missing.
struct DriftString __exc_args_get_required(const struct DriftError* err, struct DriftString key);
// Optional attr lookup via attrs (string-valued only for now).
struct DriftOptionalString __exc_attrs_get(const struct DriftError* err, struct DriftString key);

// Optional<Int> helpers for generic Optional coverage.
struct DriftOptionalInt drift_optional_int_some(int64_t value);
struct DriftOptionalInt drift_optional_int_none(void);
