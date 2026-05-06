// Minimal array runtime helpers for lang codegen tests.
// NOTE: Test-only runtime (but alignment is honored to catch layout bugs).
// This mirrors the ABI in docs/design/spec-change-requests/drift-array-lowering.md.

#include <inttypes.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../compiler_infra/error_dummy.h"

typedef struct DriftArrayHeader {
	drift_isize len;
	drift_isize cap;
	drift_isize gen;
	void *data;
} DriftArrayHeader;

__attribute__((noreturn))
void drift_bounds_check_fail(struct DriftString container_id, drift_isize idx, drift_isize len);

static unsigned char drift_zst_sentinel;
static const char k_index_error_event[] = "std.err:IndexError";
static const drift_error_code_t k_index_error_code = 1726084857549659354ULL;
static const char k_container_key[] = "container_id";
static const char k_index_key[] = "index";

static size_t drift_round_up_pow2(size_t align) {
	size_t p = 1;
	while (p < align) {
		p <<= 1;
	}
	return p;
}

// Allocate an array backing store and return a pointer to the data region.
void *drift_alloc_array(size_t elem_size, size_t elem_align, drift_isize len, drift_isize cap) {
	if (len < 0 || cap < 0) {
		fprintf(stderr, "drift_alloc_array: negative len/cap (len=%td cap=%td)\n", len, cap);
		abort();
	}
	if (cap < len) {
		cap = len;
	}
	if (elem_size == 0 || cap == 0) {
		return &drift_zst_sentinel;
	}
	if (cap != 0 && elem_size > (SIZE_MAX / (size_t)cap)) {
		fprintf(stderr, "drift_alloc_array: size overflow (elem_size=%zu cap=%td)\n", elem_size, cap);
		abort();
	}
	size_t align = elem_align;
	if (align < sizeof(void *)) {
		align = sizeof(void *);
	}
	if ((align & (align - 1)) != 0) {
		align = drift_round_up_pow2(align);
	}
	size_t bytes = elem_size * (size_t)cap;
	void *data = NULL;
	if (posix_memalign(&data, align, bytes) != 0 || !data) {
		fprintf(stderr, "drift_alloc_array: out of memory (bytes=%zu, align=%zu)\n", bytes, align);
		abort();
	}
	return data;
}

void drift_free_array(void *data) {
	if (data == &drift_zst_sentinel) {
		return;
	}
	free(data);
}

void drift_cb_env_free(void *data) {
	drift_free_array(data);
}

void *drift_iface_alloc(size_t size, size_t align) {
	return drift_alloc_array(size, align, 1, 1);
}

void drift_iface_free(void *data) {
	drift_free_array(data);
}

void drift_array_byte_commit_init_len(DriftArrayHeader *arr, drift_isize len) {
	if (len < 0 || len > arr->cap) {
		fprintf(stderr, "drift_array_byte_commit_init_len: invalid commit length (len=%td cap=%td)\n", len, arr->cap);
		abort();
	}
	arr->len = len;
}

void drift_bounds_check(struct DriftString container_id, drift_isize idx, drift_isize len) {
	if (idx < 0 || idx >= len) {
		drift_bounds_check_fail(container_id, idx, len);
	}
}

// JSON-escape a single byte into `out` (advancing the cursor).  Returns
// the number of bytes written.  Handles `"`, `\`, control chars per
// RFC 8259 §7.  Caller guarantees the buffer has at least 6 bytes of
// headroom (the worst case is `\u00XX` = 6 bytes).
static size_t drift_bcf_json_escape_byte(unsigned char b, char *out) {
	switch (b) {
	case '"':  out[0] = '\\'; out[1] = '"';  return 2;
	case '\\': out[0] = '\\'; out[1] = '\\'; return 2;
	case '\b': out[0] = '\\'; out[1] = 'b';  return 2;
	case '\f': out[0] = '\\'; out[1] = 'f';  return 2;
	case '\n': out[0] = '\\'; out[1] = 'n';  return 2;
	case '\r': out[0] = '\\'; out[1] = 'r';  return 2;
	case '\t': out[0] = '\\'; out[1] = 't';  return 2;
	default: break;
	}
	if (b < 0x20) {
		static const char hex[] = "0123456789abcdef";
		out[0] = '\\';
		out[1] = 'u';
		out[2] = '0';
		out[3] = '0';
		out[4] = hex[(b >> 4) & 0xF];
		out[5] = hex[b & 0xF];
		return 6;
	}
	out[0] = (char)b;
	return 1;
}

// Build the canonical `{"container_id":"<escaped>","index":N}`
// params JSON document into `out_buf`.  Returns bytes written, or
// -1 on overflow / -2 on bad inputs.  Factored out of
// `drift_bounds_check_fail` so the escape contract is testable from
// outside the throwing path.  See array_runtime.h.
drift_isize drift_bounds_check_params_json_build(
	struct DriftString container_id,
	drift_isize idx,
	char *out_buf,
	drift_isize out_cap) {
	if (out_buf == NULL || out_cap <= 0) return -2;
	if (container_id.len < 0) return -2;
	// Slice 7a follow-up (K finding 3 v2, 2026-05-05): NULL-data-with-
	// positive-length is undefined behavior to dereference.  Reject
	// outright rather than UB-deref `data[0]` in the escape loop.
	if (container_id.data == NULL && container_id.len > 0) return -2;
	const char prefix[] = "{\"container_id\":\"";
	const char mid[]    = "\",\"index\":";
	const char suffix[] = "}";
	const drift_isize prefix_len = (drift_isize)(sizeof(prefix) - 1);
	const drift_isize mid_len    = (drift_isize)(sizeof(mid)    - 1);
	const drift_isize suffix_len = (drift_isize)(sizeof(suffix) - 1);
	const drift_isize idx_buf_cap = 24; // max signed-decimal width
	if (out_cap < prefix_len) return -1;
	char *cursor = out_buf;
	char *end = out_buf + out_cap;
	memcpy(cursor, prefix, (size_t)prefix_len);
	cursor += prefix_len;
	for (drift_isize i = 0; i < container_id.len; i++) {
		// Worst-case escape is 6 bytes (`\u00XX`); reserve room for
		// mid + idx + suffix at the tail.
		drift_isize remaining = (drift_isize)(end - cursor);
		drift_isize tail_reserve = mid_len + idx_buf_cap + suffix_len;
		if (remaining < 6 + tail_reserve) return -1;
		cursor += drift_bcf_json_escape_byte((unsigned char)container_id.data[i], cursor);
	}
	if ((drift_isize)(end - cursor) < mid_len + idx_buf_cap + suffix_len) return -1;
	memcpy(cursor, mid, (size_t)mid_len);
	cursor += mid_len;
	int n_idx = snprintf(cursor, (size_t)idx_buf_cap, "%" PRIdPTR, (intptr_t)idx);
	if (n_idx <= 0 || n_idx >= (int)idx_buf_cap) return -1;
	cursor += n_idx;
	if ((drift_isize)(end - cursor) < suffix_len) return -1;
	memcpy(cursor, suffix, (size_t)suffix_len);
	cursor += suffix_len;
	return (drift_isize)(cursor - out_buf);
}

// Bounds check failure helper; for now, print and abort.
__attribute__((noreturn))
void drift_bounds_check_fail(struct DriftString container_id, drift_isize idx, drift_isize len) {
	(void)len;
	struct DriftString event_fqn = { (drift_isize)(sizeof(k_index_error_event) - 1), (char *)k_index_error_event };
	struct DriftError *err = drift_error_new(k_index_error_code, event_fqn);
	if (err) {
		struct DriftString container_key = { (drift_isize)(sizeof(k_container_key) - 1), (char *)k_container_key };
		struct DriftString index_key = { (drift_isize)(sizeof(k_index_key) - 1), (char *)k_index_key };
		// Slice 7a: legacy DV-attrs path retained for the internal
		// bridge (per ABI invariant — kept alive while synthesized
		// Diagnostic lowering still emits DV attaches).
		struct DriftDiagnosticValue dv_container = drift_diag_from_string(container_id);
		struct DriftDiagnosticValue dv_index = drift_diag_from_int(idx);
		drift_error_add_attr_dv(err, container_key, &dv_container);
		drift_error_add_attr_dv(err, index_key, &dv_index);
		// Slice 7a: also populate canonical params JSON so user code
		// reading via typed catch projection (`e.container_id`,
		// `e.index`) and via `e.params.encode_compact()` /
		// `e.params.get(k)` sees the values.  Lex-utf8-sorted keys —
		// container_id < index alphabetically.  Buffer sizing
		// accommodates 6× expansion of the escaped key plus the
		// 24-char signed-decimal int.  See
		// `drift_bounds_check_params_json_build` for the escape
		// contract; the helper is exposed in array_runtime.h so the
		// escaping path is independently testable.
		extern struct DriftString drift_string_from_utf8_bytes(const char* data, drift_isize len);
		// Slice 7a follow-up (K finding 3 v2, 2026-05-05): clamp the
		// computed buffer size to a sane ceiling.  An adversarial
		// container_id with len near the signed-int max would overflow
		// `len * 6` to a negative value and pass it to malloc as a huge
		// size.  In-tree callers pass stdlib container_id constants
		// shorter than 64 bytes, so the clamp never triggers in practice
		// — it's a defense-in-depth guard for any future caller.
		const drift_isize MAX_CONTAINER_ID_LEN = (drift_isize)(1 << 20); // 1 MiB
		drift_isize cid_len = container_id.len;
		if (cid_len < 0 || cid_len > MAX_CONTAINER_ID_LEN) cid_len = MAX_CONTAINER_ID_LEN;
		drift_isize pj_cap = 64 + cid_len * 6;
		char *pj_buf = (char *)malloc((size_t)pj_cap);
		if (pj_buf) {
			drift_isize n = drift_bounds_check_params_json_build(
				container_id, idx, pj_buf, pj_cap);
			if (n > 0) {
				struct DriftString params = drift_string_from_utf8_bytes(pj_buf, n);
				drift_error_set_params_json(err, params);
			}
			free(pj_buf);
		}
	}
	drift_error_raise(err);
}
