/* calibration driver for probe.drift (rev 2: classified counters —
 * wrapper calls vs real allocations vs sentinel/no-op, exact real
 * frees via a live-pointer set) */
#include "string_runtime.h"
#include <stdio.h>
#include <string.h>

static long n_alloc_calls, n_alloc_real, n_alloc_sentinel;
static long n_free_calls, n_free_real, n_free_noop;
static const void *sentinel_addr;

#define LIVE_CAP (1u << 16)
static const void *live[LIVE_CAP];
static long live_count;

static unsigned live_slot(const void *p) {
	unsigned long long h = (unsigned long long)(size_t)p;
	h ^= h >> 33; h *= 0xff51afd7ed558ccdULL; h ^= h >> 33;
	return (unsigned)(h & (LIVE_CAP - 1));
}

static void live_add(const void *p) {
	unsigned i = live_slot(p);
	while (live[i]) i = (i + 1) & (LIVE_CAP - 1);
	live[i] = p;
	live_count++;
}

static int live_remove(const void *p) {
	unsigned i = live_slot(p);
	while (live[i]) {
		if (live[i] == p) {
			unsigned j = i;
			live[i] = NULL;
			for (unsigned k = (i + 1) & (LIVE_CAP - 1); live[k];
			     k = (k + 1) & (LIVE_CAP - 1)) {
				unsigned home = live_slot(live[k]);
				if ((k > j) ? (home <= j || home > k)
				            : (home <= j && home > k)) {
					live[j] = live[k];
					live[k] = NULL;
					j = k;
				}
			}
			live_count--;
			return 1;
		}
		i = (i + 1) & (LIVE_CAP - 1);
	}
	return 0;
}

void *__real_drift_alloc_array(size_t es, size_t ea, long len, long cap);
void __real_drift_free_array(void *p);

void *__wrap_drift_alloc_array(size_t es, size_t ea, long len, long cap) {
	n_alloc_calls++;
	long eff = cap < len ? len : cap;
	void *p = __real_drift_alloc_array(es, ea, len, cap);
	if (es == 0 || eff == 0) {
		n_alloc_sentinel++;
		if (!sentinel_addr) sentinel_addr = p;
	} else {
		n_alloc_real++;
		live_add(p);
	}
	return p;
}

void __wrap_drift_free_array(void *p) {
	n_free_calls++;
	if (p && p != sentinel_addr && live_remove(p)) n_free_real++;
	else n_free_noop++;
	__real_drift_free_array(p);
}

static void reset(void) {
	n_alloc_calls = n_alloc_real = n_alloc_sentinel = 0;
	n_free_calls = n_free_real = n_free_noop = 0;
	memset((void *)live, 0, sizeof live);
	live_count = 0;
}

extern long op_wc(long);
extern long op_wc_bool(long);
extern long op_moveout(long);
extern long op_reassign(long);
extern long op_engine_shape(long);
extern long op_scratch_four(long);
extern long op_scratch_packed(long);

#define RUN(fn) do { \
	reset(); \
	long r = fn(0); \
	printf("OP=%s r=%ld alloc_calls=%ld alloc_real=%ld alloc_sentinel=%ld " \
	       "free_calls=%ld free_real=%ld free_noop=%ld live_end=%ld\n", \
	       #fn, r, n_alloc_calls, n_alloc_real, n_alloc_sentinel, \
	       n_free_calls, n_free_real, n_free_noop, live_count); \
} while (0)

int main(void) {
	RUN(op_wc);
	RUN(op_wc_bool);
	RUN(op_moveout);
	RUN(op_reassign);
	RUN(op_engine_shape);
	RUN(op_scratch_four);
	RUN(op_scratch_packed);
	printf("DONE\n");
	return 0;
}
