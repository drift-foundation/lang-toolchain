#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include "error_runtime.h"

/* Internal helper to tear down a DriftError and its owned buffers. */
static void drift_error_free(struct DriftError* derr) {
    if (!derr) return;
    if (derr->attrs) {
        for (size_t i = 0; i < derr->attr_count; i++) {
            /* attrs are read-only; keys/values are assumed owned by the error. */
        }
        free(derr->attrs);
    }
    if (derr->frames) {
        free(derr->frames);
    }
    free((void*)derr->event);
    free((void*)derr->domain);
    free(derr);
}

struct Error* drift_error_new(const char* event, const char* domain, const struct DriftErrorAttr* attrs, size_t attr_count, const struct DriftFrame* frames, size_t frame_count) {
    struct Error* wrapper = (struct Error*)malloc(sizeof(struct Error));
    if (!wrapper) return NULL;
    struct DriftError* derr = (struct DriftError*)calloc(1, sizeof(struct DriftError));
    if (!derr) { free(wrapper); return NULL; }
    /* Precompute a simple diagnostic string for error_to_cstr; owned by the error. */
    char diag_buf[256];
    diag_buf[0] = '\0';
    /* Deep-copy strings so the Error owns them. */
    if (event) {
        size_t len = strlen(event);
        char* ev = (char*)malloc(len + 1);
        if (!ev) { free(derr); free(wrapper); return NULL; }
        memcpy(ev, event, len + 1);
        derr->event = ev;
    }
    if (domain) {
        size_t len = strlen(domain);
        char* dom = (char*)malloc(len + 1);
        if (!dom) { drift_error_free(derr); free(wrapper); return NULL; }
        memcpy(dom, domain, len + 1);
        derr->domain = dom;
    }
    if (attr_count > 0 && attrs) {
        derr->attrs = (struct DriftErrorAttr*)calloc(attr_count, sizeof(struct DriftErrorAttr));
        if (!derr->attrs) { drift_error_free(derr); free(wrapper); return NULL; }
        derr->attr_count = attr_count;
        for (size_t i = 0; i < attr_count; i++) {
            const char* k = attrs[i].key;
            const char* v = attrs[i].value_json;
            if (k) {
                size_t kl = strlen(k);
                char* kcpy = (char*)malloc(kl + 1);
                if (!kcpy) { drift_error_free(derr); free(wrapper); return NULL; }
                memcpy(kcpy, k, kl + 1);
                derr->attrs[i].key = kcpy;
            }
            if (v) {
                size_t vl = strlen(v);
                char* vcpy = (char*)malloc(vl + 1);
                if (!vcpy) { drift_error_free(derr); free(wrapper); return NULL; }
                memcpy(vcpy, v, vl + 1);
                derr->attrs[i].value_json = vcpy;
            }
        }
    } else {
        /* Synthesize a single msg attr from the event text if none provided. */
        derr->attrs = (struct DriftErrorAttr*)calloc(1, sizeof(struct DriftErrorAttr));
        if (!derr->attrs) { drift_error_free(derr); free(wrapper); return NULL; }
        derr->attr_count = 1;
        derr->attrs[0].key = "msg";
        if (derr->event) {
            size_t vl = strlen(derr->event);
            /* store as {"msg":"..."} */
            size_t buf_len = vl + 10;
            char* vcpy = (char*)malloc(buf_len + 1);
            if (!vcpy) { drift_error_free(derr); free(wrapper); return NULL; }
            snprintf(vcpy, buf_len + 1, "{\"msg\":\"%s\"}", derr->event);
            derr->attrs[0].value_json = vcpy;
        } else {
            derr->attrs[0].value_json = "{\"msg\":\"unknown\"}";
        }
    }
    if (frame_count > 0 && frames) {
        derr->frames = (struct DriftFrame*)calloc(frame_count, sizeof(struct DriftFrame));
        if (!derr->frames) { drift_error_free(derr); free(wrapper); return NULL; }
        derr->frame_count = frame_count;
        for (size_t i = 0; i < frame_count; i++) {
            derr->frames[i] = frames[i];
        }
    }
    derr->free_fn = drift_error_free;
    derr->free_fn = drift_error_free;
    wrapper->inner = derr;
    return wrapper;
}

const char* error_to_cstr(struct Error* err) {
    if (!err || !err->inner) return NULL;
    const struct DriftError* derr = err->inner;
    if (derr->attr_count > 0 && derr->attrs[0].value_json) {
        return derr->attrs[0].value_json;
    }
    if (derr->event) return derr->event;
    return "<unknown>";
}

void error_free(struct Error* err) {
    if (!err) return;
    if (err->inner) {
        drift_error_free(err->inner);
    }
    free(err);
}

struct Error* error_new(const char* msg) {
    char buf[256];
    const char* m = msg ? msg : "";
    snprintf(buf, sizeof(buf), "{\"msg\":\"%s\"}", m);
    struct DriftErrorAttr attrs[1];
    attrs[0].key = "msg";
    attrs[0].value_json = buf;
    return drift_error_new("Error", NULL, attrs, 1, NULL, 0);
}
