/* string-hotpath-performance-recovery: counts + histogram driver
 * (rev 2 — review correction 1).
 *
 * Reports, per carrier window:
 *   - retain/release calls CLASSIFIED by handle kind at entry:
 *     tombstone (NULL storage) / static / immortal / heap — because
 *     release returns BEFORE the trace getenv for tombstone, static,
 *     and immortal handles, entry counts alone overstate the trace
 *     tax;
 *   - the EXACT trace multiplier: a --wrap=getenv counter keyed to
 *     the "DRIFT_STR_TRACE" name (marker-windowed like every other
 *     counter);
 *   - materialization counts + String-length histogram
 *     (from_utf8_bytes / literal / concat results);
 *   - array alloc/free call counts.
 */
#include "string_runtime.h"
#include <stdio.h>
#include <string.h>

static long n_retain_heap, n_retain_static, n_retain_immortal, n_retain_tomb;
static long n_release_heap, n_release_static, n_release_immortal, n_release_tomb;
static long n_getenv_trace, n_getenv_other;
static long n_from_utf8, n_literal, n_concat, n_alloc, n_free;
#define HCAP 129
static long hist[HCAP + 1];

static void hist_add(long len) {
	if (len < 0) return;
	if (len > HCAP) len = HCAP;
	hist[len]++;
}

/* classify by the SAME flag bits the runtime uses (relaxed load) */
static int kind_of(DriftString s) {
	if (s.storage == NULL) return 0; /* tombstone/empty-null handle */
	unsigned long long f = atomic_load_explicit(&s.storage->flags, memory_order_relaxed);
	if (f & DRIFT_RCBYTES_STATIC) return 1;
	if (f & DRIFT_RCBYTES_IMMORTAL) return 2;
	return 3; /* heap */
}

char *__real_getenv(const char *name);
DriftString __real_drift_string_retain(DriftString s);
void __real_drift_string_release(DriftString s);
DriftString __real_drift_string_from_utf8_bytes(const char *p, drift_isize len);
DriftString __real_drift_string_literal(const char *p, drift_isize len);
DriftString __real_drift_string_concat(DriftString a, DriftString b);
void *__real_drift_alloc_array(size_t es, size_t ea, long len, long cap);
void __real_drift_free_array(void *p);

char *__wrap_getenv(const char *name) {
	if (name && strcmp(name, "DRIFT_STR_TRACE") == 0) n_getenv_trace++;
	else n_getenv_other++;
	return __real_getenv(name);
}

DriftString __wrap_drift_string_retain(DriftString s) {
	switch (kind_of(s)) {
	case 0: n_retain_tomb++; break;
	case 1: n_retain_static++; break;
	case 2: n_retain_immortal++; break;
	default: n_retain_heap++; break;
	}
	return __real_drift_string_retain(s);
}

void __wrap_drift_string_release(DriftString s) {
	switch (kind_of(s)) {
	case 0: n_release_tomb++; break;
	case 1: n_release_static++; break;
	case 2: n_release_immortal++; break;
	default: n_release_heap++; break;
	}
	__real_drift_string_release(s);
}

DriftString __wrap_drift_string_from_utf8_bytes(const char *p, drift_isize len) {
	n_from_utf8++; hist_add((long)len);
	return __real_drift_string_from_utf8_bytes(p, len);
}
DriftString __wrap_drift_string_literal(const char *p, drift_isize len) {
	n_literal++; hist_add((long)len);
	return __real_drift_string_literal(p, len);
}
DriftString __wrap_drift_string_concat(DriftString a, DriftString b) {
	n_concat++; hist_add((long)(a.len + b.len));
	return __real_drift_string_concat(a, b);
}
void *__wrap_drift_alloc_array(size_t es, size_t ea, long len, long cap) { n_alloc++; return __real_drift_alloc_array(es, ea, len, cap); }
void __wrap_drift_free_array(void *p) { n_free++; __real_drift_free_array(p); }

static void reset(void) {
	n_retain_heap = n_retain_static = n_retain_immortal = n_retain_tomb = 0;
	n_release_heap = n_release_static = n_release_immortal = n_release_tomb = 0;
	n_getenv_trace = n_getenv_other = 0;
	n_from_utf8 = n_literal = n_concat = n_alloc = n_free = 0;
	memset(hist, 0, sizeof hist);
}

static void report(const char *label, long r) {
	printf("OP=%s r=%ld retain_heap=%ld retain_static=%ld retain_immortal=%ld "
	       "retain_tomb=%ld release_heap=%ld release_static=%ld "
	       "release_immortal=%ld release_tomb=%ld getenv_trace=%ld "
	       "getenv_other=%ld from_utf8=%ld literal=%ld concat=%ld "
	       "alloc_arr=%ld free_arr=%ld\n",
	       label, r, n_retain_heap, n_retain_static, n_retain_immortal,
	       n_retain_tomb, n_release_heap, n_release_static,
	       n_release_immortal, n_release_tomb, n_getenv_trace,
	       n_getenv_other, n_from_utf8, n_literal, n_concat, n_alloc, n_free);
	printf("HIST=%s", label);
	for (int i = 0; i <= HCAP; i++) {
		if (hist[i]) printf(" %d:%ld", i, hist[i]);
	}
	printf("\n");
}

extern long op_parse_once(long);
extern long op_route_once(long);

#define RUN(fn, label) do { \
	reset(); \
	long r = fn(0); \
	report(label, r); \
	if (r < 0) { printf("OPFAIL=%s\n", label); return 70; } \
} while (0)

int main(void) {
	RUN(op_parse_once, "parse_x1000");
	RUN(op_route_once, "route_x1000");
	printf("DONE\n");
	return 0;
}
