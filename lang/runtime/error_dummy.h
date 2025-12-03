// Minimal dummy error constructor for SSA error-path testing.
#pragma once

#include <stdint.h>
#include <stddef.h>
#include "string_runtime.h"

#define DRIFT_EVENT_KIND_TEST 0
#define DRIFT_EVENT_PAYLOAD_MASK ((1ULL << 60) - 1)

struct DriftErrorArg {
    struct DriftString key;
    struct DriftString value;
};

struct DriftOptionalString {
    uint8_t is_some;
    struct DriftString value;
};
#define OPTIONAL_STRING_NONE \
    (struct DriftOptionalString){.is_some = 0, .value = (struct DriftString){0, NULL}}

struct DriftOptionalInt {
    uint8_t is_some;
    int64_t value;
};
#define OPTIONAL_INT_NONE (struct DriftOptionalInt){.is_some = 0, .value = 0}

struct DriftError {
    int64_t code;               // matches Drift Int (word-sized)
    struct DriftString payload; // legacy first payload field (if provided)
    struct DriftErrorArg* args; // dynamic array of args (key/value)
    size_t arg_count;           // number of entries in args
};

// Returns a non-null Error* for testing error-edge lowering.
struct DriftError* drift_error_new_dummy(int64_t code, struct DriftString key, struct DriftString payload);
int64_t drift_error_get_code(struct DriftError* err);
// Returns pointer to value if found, NULL otherwise. No ownership transfer.
const struct DriftString* drift_error_get_arg(const struct DriftError* err, const struct DriftString* key);
// Append an arg (key,value) to an existing error.
void drift_error_add_arg(struct DriftError* err, struct DriftString key, struct DriftString value);
// Optional<String> return for exception arg lookup.
struct DriftOptionalString __exc_args_get(const struct DriftError* err, struct DriftString key);
// Required arg lookup: returns empty string if missing (used for typed catches where the field is guaranteed).
struct DriftString __exc_args_get_required(const struct DriftError* err, struct DriftString key);

// Optional<Int> helpers for generic Optional coverage.
struct DriftOptionalInt drift_optional_int_some(int64_t value);
struct DriftOptionalInt drift_optional_int_none(void);
