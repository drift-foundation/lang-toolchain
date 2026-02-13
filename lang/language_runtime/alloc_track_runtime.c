#include <stddef.h>
#include <stdint.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#if defined(__linux__) || defined(__GLIBC__)
#include <malloc.h>
#define DRIFT_HAVE_MALLOC_USABLE_SIZE 1
#else
#define DRIFT_HAVE_MALLOC_USABLE_SIZE 0
#endif

#if defined(DRIFT_ALLOC_WRAP_ENABLED)
void *__real_malloc(size_t size);
void *__real_calloc(size_t nmemb, size_t size);
void *__real_realloc(void *ptr, size_t size);
void __real_free(void *ptr);
int __real_posix_memalign(void **memptr, size_t alignment, size_t size);
void *__real_aligned_alloc(size_t alignment, size_t size);
#endif

static _Atomic uint64_t drift_alloc_count = 0;
static _Atomic uint64_t drift_free_count = 0;
static _Atomic uint64_t drift_realloc_count = 0;
static _Atomic uint64_t drift_live_blocks = 0;
static _Atomic uint64_t drift_live_bytes = 0;
static _Atomic uint64_t drift_peak_live_bytes = 0;
static _Atomic int drift_track_enabled = 0;
static _Atomic int drift_track_init = 0;
extern void drift_runtime_registry_cleanup_now(void) __attribute__((weak));

static uint64_t drift_alloc_size_of_ptr(void *ptr) {
	if (ptr == NULL) {
		return 0;
	}
#if DRIFT_HAVE_MALLOC_USABLE_SIZE
	return (uint64_t)malloc_usable_size(ptr);
#else
	(void)ptr;
	return 0;
#endif
}

static void drift_alloc_update_peak(uint64_t live_now) {
	uint64_t peak = atomic_load_explicit(&drift_peak_live_bytes, memory_order_relaxed);
	while (live_now > peak) {
		if (atomic_compare_exchange_weak_explicit(
			&drift_peak_live_bytes,
			&peak,
			live_now,
			memory_order_relaxed,
			memory_order_relaxed
		)) {
			break;
		}
	}
}

static void drift_alloc_init_once(void) {
	int expected = 0;
	if (!atomic_compare_exchange_strong_explicit(
		&drift_track_init,
		&expected,
		1,
		memory_order_relaxed,
		memory_order_relaxed
	)) {
		return;
	}
	const char *raw = getenv("DRIFT_ALLOC_TRACK");
	if (raw != NULL && (strcmp(raw, "1") == 0 || strcmp(raw, "true") == 0 || strcmp(raw, "True") == 0)) {
		atomic_store_explicit(&drift_track_enabled, 1, memory_order_relaxed);
	}
}

static void drift_alloc_report(void) {
	if (drift_runtime_registry_cleanup_now) {
		drift_runtime_registry_cleanup_now();
	}
	if (atomic_load_explicit(&drift_track_enabled, memory_order_relaxed) == 0) {
		return;
	}
	char buf[512];
	const uint64_t allocs = atomic_load_explicit(&drift_alloc_count, memory_order_relaxed);
	const uint64_t frees = atomic_load_explicit(&drift_free_count, memory_order_relaxed);
	const uint64_t reallocs = atomic_load_explicit(&drift_realloc_count, memory_order_relaxed);
	const uint64_t live_blocks = atomic_load_explicit(&drift_live_blocks, memory_order_relaxed);
	const uint64_t live_bytes = atomic_load_explicit(&drift_live_bytes, memory_order_relaxed);
	const uint64_t peak = atomic_load_explicit(&drift_peak_live_bytes, memory_order_relaxed);
	const int n = snprintf(
		buf,
		sizeof(buf),
		"__DRIFT_ALLOC_TRACK__ {\"alloc_count\":%llu,\"free_count\":%llu,\"realloc_count\":%llu,\"live_blocks\":%llu,\"live_bytes\":%llu,\"peak_live_bytes\":%llu}\n",
		(unsigned long long)allocs,
		(unsigned long long)frees,
		(unsigned long long)reallocs,
		(unsigned long long)live_blocks,
		(unsigned long long)live_bytes,
		(unsigned long long)peak
	);
	if (n > 0) {
		(void)write(STDERR_FILENO, buf, (size_t)n);
	}
}

void drift_alloc_report_now(void) {
	drift_alloc_report();
}

__attribute__((constructor))
static void drift_alloc_ctor(void) {
	drift_alloc_init_once();
	(void)atexit(drift_alloc_report);
}

#if defined(DRIFT_ALLOC_WRAP_ENABLED)
void *__wrap_malloc(size_t size) {
	drift_alloc_init_once();
	void *ptr = __real_malloc(size);
	if (ptr != NULL && atomic_load_explicit(&drift_track_enabled, memory_order_relaxed) != 0) {
		atomic_fetch_add_explicit(&drift_alloc_count, 1, memory_order_relaxed);
		atomic_fetch_add_explicit(&drift_live_blocks, 1, memory_order_relaxed);
		uint64_t sz = drift_alloc_size_of_ptr(ptr);
		uint64_t live_now = atomic_fetch_add_explicit(&drift_live_bytes, sz, memory_order_relaxed) + sz;
		drift_alloc_update_peak(live_now);
	}
	return ptr;
}

void *__wrap_calloc(size_t nmemb, size_t size) {
	drift_alloc_init_once();
	void *ptr = __real_calloc(nmemb, size);
	if (ptr != NULL && atomic_load_explicit(&drift_track_enabled, memory_order_relaxed) != 0) {
		atomic_fetch_add_explicit(&drift_alloc_count, 1, memory_order_relaxed);
		atomic_fetch_add_explicit(&drift_live_blocks, 1, memory_order_relaxed);
		uint64_t sz = drift_alloc_size_of_ptr(ptr);
		uint64_t live_now = atomic_fetch_add_explicit(&drift_live_bytes, sz, memory_order_relaxed) + sz;
		drift_alloc_update_peak(live_now);
	}
	return ptr;
}

void *__wrap_realloc(void *ptr, size_t size) {
	drift_alloc_init_once();
	uint64_t old_sz = 0;
	int had_ptr = (ptr != NULL);
	if (had_ptr && atomic_load_explicit(&drift_track_enabled, memory_order_relaxed) != 0) {
		old_sz = drift_alloc_size_of_ptr(ptr);
	}
	void *new_ptr = __real_realloc(ptr, size);
	if (atomic_load_explicit(&drift_track_enabled, memory_order_relaxed) != 0) {
		atomic_fetch_add_explicit(&drift_realloc_count, 1, memory_order_relaxed);
		if (!had_ptr && new_ptr != NULL) {
			atomic_fetch_add_explicit(&drift_alloc_count, 1, memory_order_relaxed);
			atomic_fetch_add_explicit(&drift_live_blocks, 1, memory_order_relaxed);
			uint64_t new_sz = drift_alloc_size_of_ptr(new_ptr);
			uint64_t live_now = atomic_fetch_add_explicit(&drift_live_bytes, new_sz, memory_order_relaxed) + new_sz;
			drift_alloc_update_peak(live_now);
		} else if (had_ptr && new_ptr != NULL) {
			uint64_t new_sz = drift_alloc_size_of_ptr(new_ptr);
			if (new_sz >= old_sz) {
				uint64_t delta = new_sz - old_sz;
				uint64_t live_now = atomic_fetch_add_explicit(&drift_live_bytes, delta, memory_order_relaxed) + delta;
				drift_alloc_update_peak(live_now);
			} else {
				atomic_fetch_sub_explicit(&drift_live_bytes, old_sz - new_sz, memory_order_relaxed);
			}
		}
	}
	return new_ptr;
}

void __wrap_free(void *ptr) {
	drift_alloc_init_once();
	if (ptr != NULL && atomic_load_explicit(&drift_track_enabled, memory_order_relaxed) != 0) {
		atomic_fetch_add_explicit(&drift_free_count, 1, memory_order_relaxed);
		atomic_fetch_sub_explicit(&drift_live_blocks, 1, memory_order_relaxed);
		uint64_t sz = drift_alloc_size_of_ptr(ptr);
		atomic_fetch_sub_explicit(&drift_live_bytes, sz, memory_order_relaxed);
	}
	__real_free(ptr);
}

int __wrap_posix_memalign(void **memptr, size_t alignment, size_t size) {
	drift_alloc_init_once();
	const int rc = __real_posix_memalign(memptr, alignment, size);
	if (rc == 0 && memptr != NULL && *memptr != NULL && atomic_load_explicit(&drift_track_enabled, memory_order_relaxed) != 0) {
		atomic_fetch_add_explicit(&drift_alloc_count, 1, memory_order_relaxed);
		atomic_fetch_add_explicit(&drift_live_blocks, 1, memory_order_relaxed);
		uint64_t sz = drift_alloc_size_of_ptr(*memptr);
		uint64_t live_now = atomic_fetch_add_explicit(&drift_live_bytes, sz, memory_order_relaxed) + sz;
		drift_alloc_update_peak(live_now);
	}
	return rc;
}

void *__wrap_aligned_alloc(size_t alignment, size_t size) {
	drift_alloc_init_once();
	void *ptr = __real_aligned_alloc(alignment, size);
	if (ptr != NULL && atomic_load_explicit(&drift_track_enabled, memory_order_relaxed) != 0) {
		atomic_fetch_add_explicit(&drift_alloc_count, 1, memory_order_relaxed);
		atomic_fetch_add_explicit(&drift_live_blocks, 1, memory_order_relaxed);
		uint64_t sz = drift_alloc_size_of_ptr(ptr);
		uint64_t live_now = atomic_fetch_add_explicit(&drift_live_bytes, sz, memory_order_relaxed) + sz;
		drift_alloc_update_peak(live_now);
	}
	return ptr;
}
#endif
