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
    // allocation class.  This handles all caller patterns uniformly:
    //   - heap DriftString (refcount=1) — input ref kept by caller.
    //   - static-flagged DriftString (LLVM-emitted literal) — caller's
    //     reference is permanent; runtime owns its own copy.
    //   - raw cstring DriftString (e.g. lang/language_runtime/array_runtime.c:106
    //     constructs `{ len, (char *)k_event }` over a static C literal
    //     with no DriftStringHeader at all).  drift_string_from_utf8_bytes
    //     copies the bytes into a properly-headered DriftString,
    //     decoupling the runtime's storage from the caller's input.
    // drift_error_release safely drops the runtime's owned copy.
    err->event_fqn = drift_string_from_utf8_bytes(event_fqn.data, event_fqn.len);
    err->attrs = NULL;
    err->attr_count = 0;
    err->frames = NULL;
    err->frame_count = 0;
    // Phase 1 additive: initialize JSON segments to empty canonical form
    // ("{}" / "[]") so getters always observe well-formed JSON.
    err->params_json = drift_string_from_cstr("{}");
    err->context_json = drift_string_from_cstr("[]");
    (void)key;
    (void)payload;
    return err;
}

void drift_error_add_attr_dv(struct DriftError* err, struct DriftString key, const struct DriftDiagnosticValue* value) {
    if (!err) {
        return;
    }
    size_t new_count = err->attr_count + 1;
    struct DriftErrorAttr* new_attrs = realloc(err->attrs, new_count * sizeof(struct DriftErrorAttr));
    if (!new_attrs) {
        abort();
    }
    new_attrs[new_count - 1].key = drift_string_retain(key);
    new_attrs[new_count - 1].value = drift_dv_clone(value);
#ifdef DEBUG_DIAGNOSTICS
    if (value) {
        fprintf(stderr, "[err_add_attr] count=%zu key=%.*s tag=%u len=%lld ptr=%p\n",
                new_count,
                (int)key.len, key.data ? key.data : "<null>",
                value->tag,
                (long long)value->data.string_value.len,
                (void*)value->data.string_value.data);
        fflush(stderr);
    }
#endif
    err->attrs = new_attrs;
    err->attr_count = new_count;
}

void drift_error_add_local_dv(struct DriftError* err, struct DriftString frame, struct DriftString key, const struct DriftDiagnosticValue* value) {
    if (!err) return;
    size_t frame_idx = err->frame_count;
    for (size_t i = 0; i < err->frame_count; i++) {
        if (err->frames[i].name.len == frame.len && (frame.len == 0 || memcmp(err->frames[i].name.data, frame.data, frame.len) == 0)) {
            frame_idx = i;
            break;
        }
    }
    if (frame_idx == err->frame_count) {
        size_t new_count = err->frame_count + 1;
        struct DriftCtxFrame* new_frames = realloc(err->frames, new_count * sizeof(struct DriftCtxFrame));
        if (!new_frames) abort();
        new_frames[new_count - 1].name = drift_string_retain(frame);
        new_frames[new_count - 1].locals = NULL;
        new_frames[new_count - 1].local_count = 0;
        err->frames = new_frames;
        err->frame_count = new_count;
    }
    struct DriftCtxFrame* tgt = &err->frames[frame_idx];
    size_t new_lcount = tgt->local_count + 1;
    struct DriftErrorLocal* new_locals = realloc(tgt->locals, new_lcount * sizeof(struct DriftErrorLocal));
    if (!new_locals) abort();
    new_locals[new_lcount - 1].key = drift_string_retain(key);
    new_locals[new_lcount - 1].value = value ? drift_dv_clone(value) : drift_dv_missing();
    tgt->locals = new_locals;
    tgt->local_count = new_lcount;
}

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

const struct DriftDiagnosticValue* drift_error_get_attr(const struct DriftError* err, const struct DriftString* key) {
    if (!err || !key) return NULL;
    for (size_t i = 0; i < err->attr_count; i++) {
        struct DriftErrorAttr* entry = &err->attrs[i];
        if (entry->key.len == key->len && (key->len == 0 || memcmp(entry->key.data, key->data, key->len) == 0)) {
            return &entry->value;
        }
    }
    return NULL;
}

uint8_t __exc_attrs_get(struct DriftString* out, const struct DriftError* err, struct DriftString key) {
    if (!err) {
        return 0;
    }
    const struct DriftDiagnosticValue* val = drift_error_get_attr(err, &key);
    if (!val || val->tag != DV_STRING) {
        return 0;
    }
    if (out) {
        out->len = val->data.string_value.len;
        out->data = val->data.string_value.data;
    }
    return 1;
}

void __exc_attrs_get_dv(struct DriftDiagnosticValue* out, const struct DriftError* err, struct DriftString key) {
    if (!out) return;
    *out = drift_dv_missing();
    if (!err) {
        return;
    }
    const struct DriftDiagnosticValue* val = drift_error_get_attr(err, &key);
    if (!val) {
        return;
    }
    // The returned DriftDiagnosticValue must be an INDEPENDENT owner
    // of any inner refcounted storage (e.g. DV_STRING).  A shallow
    // `*out = *val` aliases the exception's attribute storage and
    // double-releases when both the user-side DV and the exception
    // tear down — heap corruption when the same exception type is
    // thrown more than once with heap-built String fields.
    *out = drift_dv_clone(val);
}

void __exc_captures_get_dv(struct DriftDiagnosticValue* out, const struct DriftError* err, struct DriftString frame, struct DriftString key) {
    if (!out) return;
    *out = drift_dv_missing();
    if (!err) {
        return;
    }
    for (size_t i = 0; i < err->frame_count; i++) {
        const struct DriftCtxFrame* fr = &err->frames[i];
        if (fr->name.len != frame.len) {
            continue;
        }
        if (frame.len != 0 && memcmp(fr->name.data, frame.data, frame.len) != 0) {
            continue;
        }
        for (size_t j = 0; j < fr->local_count; j++) {
            const struct DriftErrorLocal* loc = &fr->locals[j];
            if (loc->key.len != key.len) {
                continue;
            }
            if (key.len != 0 && memcmp(loc->key.data, key.data, key.len) != 0) {
                continue;
            }
            // Same ownership-clone requirement as __exc_attrs_get_dv —
            // the user-side DV must independently own any refcounted
            // inner storage to avoid double-release on teardown.
            *out = drift_dv_clone(&loc->value);
            return;
        }
        return;
    }
}

struct DriftError* drift_error_new_with_payload(drift_error_code_t code, struct DriftString event_fqn, struct DriftString key, const struct DriftDiagnosticValue* payload) {
    struct DriftError* err = drift_error_new_dummy(code, event_fqn, (struct DriftString){0, NULL}, (struct DriftString){0, NULL});
    if (!err) {
        return NULL;
    }
    if (payload) {
        drift_error_add_attr_dv(err, key, payload);
    }
    return err;
}

struct DriftError* drift_error_new(drift_error_code_t code, struct DriftString event_fqn) {
    struct DriftError* err = drift_error_new_dummy(code, event_fqn, (struct DriftString){0, NULL}, (struct DriftString){0, NULL});
    return err;
}

void drift_error_release(struct DriftError* err) {
    if (!err) {
        return;
    }
    for (size_t i = 0; i < err->frame_count; i++) {
        struct DriftCtxFrame* frame = &err->frames[i];
        for (size_t j = 0; j < frame->local_count; j++) {
            drift_string_release(frame->locals[j].key);
            drift_dv_release(&frame->locals[j].value);
        }
        free(frame->locals);
        frame->locals = NULL;
        frame->local_count = 0;
        drift_string_release(frame->name);
    }
    free(err->frames);
    err->frames = NULL;
    err->frame_count = 0;
    for (size_t i = 0; i < err->attr_count; i++) {
        drift_string_release(err->attrs[i].key);
        drift_dv_release(&err->attrs[i].value);
    }
    free(err->attrs);
    err->attrs = NULL;
    err->attr_count = 0;
    // Phase 1: release event_fqn alongside JSON segments.  The runtime
    // holds its own owned copy of event_fqn (allocated at
    // drift_error_new_dummy via drift_string_from_utf8_bytes — see
    // drift-lang-abi.md §2.3 ownership contract), independent of the
    // caller's input.  Releasing it here is always safe regardless of
    // the caller's allocation class.
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
