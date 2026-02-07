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
__attribute__((noreturn))
void drift_bounds_check_fail(struct DriftString container_id, drift_isize idx, drift_isize len);

#endif // LANG2_ARRAY_RUNTIME_H
