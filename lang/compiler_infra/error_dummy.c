#include "error_dummy.h"
#include <stdlib.h>
#include <stdio.h>
#include <string.h>

extern struct DriftString drift_string_retain(struct DriftString s);
extern void drift_string_release(struct DriftString s);
extern struct DriftString drift_string_from_cstr(const char *cstr);
extern struct DriftString drift_string_from_utf8_bytes(const char *data, drift_isize len);

struct DriftError* drift_error_new_dummy(drift_error_code_t code, struct DriftString event_fqn, struct DriftString key, struct DriftString payload) {
    struct DriftError* err = malloc(sizeof(struct DriftError));
    if (!err) {
        abort();
    }
    err->code = code;
    // Robustness contract: the runtime allocates its own owned copy of
    // event_fqn so callers don't have to know the input string's
    // allocation class (heap / static / raw cstring all handled
    // uniformly).  drift_error_release safely drops the runtime's
    // owned copy.
    err->event_fqn = drift_string_from_utf8_bytes(event_fqn.data, event_fqn.len);
    // Initialize JSON segments to empty canonical form ("{}" / "[]")
    // so getters always observe well-formed JSON.
    err->params_json = drift_string_from_cstr("{}");
    err->context_json = drift_string_from_cstr("[]");
    (void)key;
    (void)payload;
    return err;
}

// Slice 7c-1 (ABI 14, 2026-05-06): the legacy DV-attachment helpers
// (`drift_error_add_attr_dv`, `drift_error_add_local_dv`,
// `__exc_attrs_get_dv`, `__exc_captures_get_dv`,
// `drift_error_new_with_payload`, `drift_error_get_attr`) are
// deleted.  Old (ABI 13) binaries that still reference these symbols
// will fail to link against the ABI 14 archive.

drift_error_code_t drift_error_get_code(struct DriftError* err) {
    if (!err) return 0;
    return err->code;
}

struct DriftString drift_error_get_event_fqn(const struct DriftError* err) {
    if (!err) {
        struct DriftString empty = {0, NULL};
        return empty;
    }
    return err->event_fqn;
}

uint8_t __exc_attrs_get(struct DriftString* out, const struct DriftError* err, struct DriftString key) {
    // Slice 7c-1 (ABI 14): the legacy `__exc_attrs_get_dv` and the
    // typed-attrs lookup that backed `__exc_attrs_get` are gone —
    // the params JSON path is the sole source of attr values now.
    // This entry point is retained as a soft no-op so any
    // codegen-emitted call that slipped past the type-checker
    // (shouldn't happen, but defense-in-depth) returns "no attr".
    (void)err;
    (void)key;
    if (out) {
        out->len = 0;
        out->data = NULL;
    }
    return 0;
}

struct DriftError* drift_error_new(drift_error_code_t code, struct DriftString event_fqn) {
    struct DriftError* err = drift_error_new_dummy(code, event_fqn, (struct DriftString){0, NULL}, (struct DriftString){0, NULL});
    return err;
}

void drift_error_release(struct DriftError* err) {
    if (!err) {
        return;
    }
    // Slice 7c-1 (ABI 14): legacy `attrs` / `frames` storage (and
    // the matching DV-release loops) is gone — the DriftError struct
    // no longer carries those fields.  Only `event_fqn` /
    // `params_json` / `context_json` need releasing.
    drift_string_release(err->event_fqn);
    err->event_fqn = (struct DriftString){0, NULL};
    drift_string_release(err->params_json);
    err->params_json = (struct DriftString){0, NULL};
    drift_string_release(err->context_json);
    err->context_json = (struct DriftString){0, NULL};
    free(err);
}

void drift_error_raise(struct DriftError* err) {
    (void)err;
    abort();
}

// ─────────────────────────────────────────────────────────────────
// Phase 1 additive JSON helpers (drift-lang-abi.md §2.3).
//
// `params_json` / `context_json` storage is a refcounted DriftString
// owned solely by the runtime.  Helpers preserve byte-exact contents
// of caller-provided buffers (ABI §2.2 fastpath guarantee — the
// language-level e.encode_compact() splices these directly without
// parse/re-encode).
// ─────────────────────────────────────────────────────────────────

void drift_error_set_params_json(struct DriftError* err, struct DriftString params_json) {
    if (!err) {
        // Caller-provided ownership cannot be honoured if err is NULL —
        // release to keep the contract symmetric (caller transferred in,
        // ownership ends here).
        drift_string_release(params_json);
        return;
    }
    // Take ownership of the new value (no clone — refcount transferred
    // in from caller).  Replacement releases the prior value exactly
    // once via drift_string_release.
    struct DriftString prior = err->params_json;
    err->params_json = params_json;
    drift_string_release(prior);
}

// Build "[" + frame + "]" or splice " ,frame" before the trailing "]"
// of the existing context_json, producing a fresh owned DriftString.
// Caller transfers ownership of frame_json in; runtime releases it
// after splicing (its bytes are copied into the merged buffer).
void drift_error_append_context_frame(struct DriftError* err, struct DriftString frame_json) {
    if (!err) {
        drift_string_release(frame_json);
        return;
    }
    struct DriftString prior = err->context_json;
    // Shape invariant: prior is a well-formed canonical JSON array
    // string.  Empty form is exactly "[]" (len == 2).  Non-empty form
    // ends with "]" at prior.data[prior.len - 1].
    int prior_is_empty = (prior.len == 2);
    drift_isize body_len = prior.len - 2;  // strip leading "[" and trailing "]"
    if (body_len < 0) {
        // Defensive: malformed prior — re-initialize before splice.
        body_len = 0;
        prior_is_empty = 1;
    }
    drift_isize need_comma = prior_is_empty ? 0 : 1;
    drift_isize merged_len = 1 /* "[" */ + body_len + need_comma + frame_json.len + 1 /* "]" */;

    // Build the merged document in a temporary buffer, then hand the
    // bytes to the string runtime to allocate a properly-headered
    // DriftString.  Hand-rolling a DriftStringHeader here would couple
    // this translation unit to the string-runtime layout.
    char* tmp = malloc((size_t)merged_len + 1);
    if (!tmp) {
        drift_string_release(frame_json);
        return;
    }
    drift_isize pos = 0;
    tmp[pos++] = '[';
    if (!prior_is_empty && body_len > 0) {
        memcpy(tmp + pos, prior.data + 1, (size_t)body_len);
        pos += body_len;
        tmp[pos++] = ',';
    }
    if (frame_json.len > 0) {
        memcpy(tmp + pos, frame_json.data, (size_t)frame_json.len);
        pos += frame_json.len;
    }
    tmp[pos++] = ']';
    tmp[pos] = '\0';

    extern struct DriftString drift_string_from_utf8_bytes(const char* data, drift_isize len);
    struct DriftString merged = drift_string_from_utf8_bytes(tmp, merged_len);
    free(tmp);

    err->context_json = merged;
    drift_string_release(prior);
    drift_string_release(frame_json);
}

struct DriftString drift_error_get_params_json(const struct DriftError* err) {
    if (!err) {
        struct DriftString empty = {0, NULL};
        return empty;
    }
    return drift_string_retain(err->params_json);
}

struct DriftString drift_error_get_context_json(const struct DriftError* err) {
    if (!err) {
        struct DriftString empty = {0, NULL};
        return empty;
    }
    return drift_string_retain(err->context_json);
}
