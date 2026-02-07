// Minimal array runtime helpers for lang codegen tests.
// NOTE: Test-only runtime (but alignment is honored to catch layout bugs).
// This mirrors the ABI in docs/design/spec-change-requests/drift-array-lowering.md.

#include <inttypes.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

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

void drift_bounds_check(struct DriftString container_id, drift_isize idx, drift_isize len) {
	if (idx < 0 || idx >= len) {
		drift_bounds_check_fail(container_id, idx, len);
	}
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
		struct DriftDiagnosticValue dv_container = drift_diag_from_string(container_id);
		struct DriftDiagnosticValue dv_index = drift_diag_from_int(idx);
		drift_error_add_attr_dv(err, container_key, &dv_container);
		drift_error_add_attr_dv(err, index_key, &dv_index);
	}
	drift_error_raise(err);
}
