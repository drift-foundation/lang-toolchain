#ifndef LANG2_ARRAY_RUNTIME_H
#define LANG2_ARRAY_RUNTIME_H

#include <stddef.h>
#include <stdint.h>

typedef ptrdiff_t drift_isize;
typedef size_t drift_usize;
_Static_assert(sizeof(drift_isize) == sizeof(void *), "drift_isize must be pointer-sized");
_Static_assert(sizeof(drift_usize) == sizeof(void *), "drift_usize must be pointer-sized");

typedef struct DriftArrayHeader {
	drift_isize len;
	drift_isize cap;
	drift_isize gen;
	void *data;
} DriftArrayHeader;

struct DriftString;

void *drift_alloc_array(size_t elem_size, size_t elem_align, drift_isize len, drift_isize cap);
void drift_free_array(void *data);
void drift_cb_env_free(void *data);
void *drift_iface_alloc(size_t size, size_t align);
void drift_iface_free(void *data);
void drift_bounds_check(struct DriftString container_id, drift_isize idx, drift_isize len);
void drift_array_byte_commit_init_len(DriftArrayHeader *arr, drift_isize len);
__attribute__((noreturn))
void drift_bounds_check_fail(struct DriftString container_id, drift_isize idx, drift_isize len);

// Slice 7a follow-up (K finding 3, 2026-05-05): factor the IndexError
// params-JSON builder out of `drift_bounds_check_fail` into a
// separately-testable helper.  Builds the canonical
// `{"container_id":"<escaped>","index":N}` document into `out_buf` and
// returns the byte length written, or -1 on overflow / -2 on bad
// inputs.  The container_id is JSON-string-escaped (RFC 8259 §7);
// callers in the production runtime do not currently pass strings
// with `"`, `\`, or control bytes, but the helper must be safe for
// any UTF-8 input.  Caller owns `out_buf`; capacity must accommodate
// 6× expansion of `container_id.len` (worst-case `\u00XX` per byte)
// plus a 24-char signed decimal int plus the literal scaffolding.
drift_isize drift_bounds_check_params_json_build(
	struct DriftString container_id,
	drift_isize idx,
	char *out_buf,
	drift_isize out_cap);

#endif // LANG2_ARRAY_RUNTIME_H
