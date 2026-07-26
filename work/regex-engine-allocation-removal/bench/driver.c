/* regex-engine-allocation-removal: count-exact C driver (rev 2).
 * -Wl,--wrap counting shim over the cross-TU runtime calls made by
 * generated IR.  Reports SEPARATELY (reviewer blocker 1):
 *   - wrapper invocations (alloc_calls / free_calls)
 *   - real allocations (alloc_real: elem_size>0 && max(len,cap)>0 —
 *     verified by capturing the sentinel address the first time a
 *     zero-capacity call returns it)
 *   - real frees (free_real: pointer present in a live-set of real
 *     allocation results — EXACT, not inferred)
 *   - sentinel/no-op calls (alloc_sentinel / free_noop)
 * Marker windows: reset() before each op (also clears the live-set),
 * report() after; compilation subtracted via compile-only twin rows. */
#include "string_runtime.h"
#include <stdio.h>
#include <string.h>

static long n_retain, n_release, n_release_real, n_release_null, n_from_utf8;
static long n_alloc_calls, n_alloc_real, n_alloc_sentinel;
static long n_free_calls, n_free_real, n_free_noop;
static const void *sentinel_addr;

/* open-addressing live-pointer set (live count is small; churn high) */
#define LIVE_CAP (1u << 20)
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
	if (live_count > (long)(LIVE_CAP / 2)) {
		fprintf(stderr, "live-set overflow\n");
	}
}

static int live_remove(const void *p) {
	unsigned i = live_slot(p);
	while (live[i]) {
		if (live[i] == p) {
			/* backward-shift deletion for linear probing */
			unsigned j = i;
			live[i] = NULL;
			for (unsigned k = (i + 1) & (LIVE_CAP - 1); live[k];
			     k = (k + 1) & (LIVE_CAP - 1)) {
				unsigned home = live_slot(live[k]);
				/* does live[k] belong at or before j (cyclically)? */
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

DriftString __real_drift_string_retain(DriftString s);
void __real_drift_string_release(DriftString s);
DriftString __real_drift_string_from_utf8_bytes(const char *p, drift_isize len);
void *__real_drift_alloc_array(size_t es, size_t ea, long len, long cap);
void __real_drift_free_array(void *p);

DriftString __wrap_drift_string_retain(DriftString s) { n_retain++; return __real_drift_string_retain(s); }
void __wrap_drift_string_release(DriftString s) {
	n_release++;
	if (s.storage) n_release_real++; else n_release_null++;
	__real_drift_string_release(s);
}
DriftString __wrap_drift_string_from_utf8_bytes(const char *p, drift_isize len) { n_from_utf8++; return __real_drift_string_from_utf8_bytes(p, len); }

void *__wrap_drift_alloc_array(size_t es, size_t ea, long len, long cap) {
	n_alloc_calls++;
	long eff = cap < len ? len : cap;
	void *p = __real_drift_alloc_array(es, ea, len, cap);
	if (es == 0 || eff == 0) {
		n_alloc_sentinel++;
		if (!sentinel_addr) sentinel_addr = p;
		else if (sentinel_addr != p) fprintf(stderr, "sentinel addr changed!\n");
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
	n_retain = n_release = n_release_real = n_release_null = n_from_utf8 = 0;
	n_alloc_calls = n_alloc_real = n_alloc_sentinel = 0;
	n_free_calls = n_free_real = n_free_noop = 0;
	memset((void *)live, 0, sizeof live);
	live_count = 0;
}

static void report(const char *label, long r) {
	printf("OP=%s r=%ld retain=%ld release=%ld release_real=%ld "
	       "release_null=%ld from_utf8=%ld "
	       "alloc_calls=%ld alloc_real=%ld alloc_sentinel=%ld "
	       "free_calls=%ld free_real=%ld free_noop=%ld live_end=%ld\n",
	       label, r, n_retain, n_release, n_release_real, n_release_null,
	       n_from_utf8,
	       n_alloc_calls, n_alloc_real, n_alloc_sentinel,
	       n_free_calls, n_free_real, n_free_noop, live_count);
}

extern DriftString mk_carrier_64k(void);
extern DriftString mk_carrier_2m(void);
extern DriftString mk_nomatch_64k(void);
extern DriftString mk_nomatch_2m(void);
extern DriftString mk_zw(void);
extern DriftString mk_short_hit(void);
extern DriftString mk_short_miss(void);
extern DriftString mk_anchor_in(void);

extern long op_compile_p1(DriftString);
extern long op_compile_alt(DriftString);
extern long op_compile_zw(DriftString);
extern long op_compile_anchor(DriftString);
extern long op_scan_all(DriftString);
extern long op_find_nomatch(DriftString);
extern long op_find_nomatch_view(DriftString);
extern long op_alt(DriftString);
extern long op_zw(DriftString);
extern long op_short_hit(DriftString);
extern long op_short_miss(DriftString);
extern long op_anchor(DriftString);

#define RUN(fn, subj, label) do { \
	DriftString a = drift_string_retain(subj); \
	reset(); \
	long r = fn(a); \
	report(label, r); \
	if (r < 0) { printf("OPFAIL=%s r=%ld\n", label, r); return 70; } \
} while (0)

int main(void) {
	DriftString carrier64 = mk_carrier_64k();
	DriftString carrier2m = mk_carrier_2m();
	DriftString nomatch64 = mk_nomatch_64k();
	DriftString nomatch2m = mk_nomatch_2m();
	DriftString zw = mk_zw();
	DriftString short_hit = mk_short_hit();
	DriftString short_miss = mk_short_miss();
	DriftString anchor_in = mk_anchor_in();

	RUN(op_compile_p1, short_hit, "compile_p1");
	RUN(op_compile_alt, short_hit, "compile_alt");
	RUN(op_compile_zw, short_hit, "compile_zw");
	RUN(op_compile_anchor, short_hit, "compile_anchor");

	RUN(op_scan_all, carrier64, "scan_all_64k");
	RUN(op_scan_all, carrier2m, "scan_all_2m");
	RUN(op_find_nomatch, nomatch64, "find_nomatch_64k");
	RUN(op_find_nomatch, nomatch2m, "find_nomatch_2m");
	RUN(op_find_nomatch_view, nomatch64, "find_nomatch_view_64k");
	RUN(op_alt, nomatch64, "alt_64k");
	RUN(op_zw, zw, "zw_x100");
	RUN(op_short_hit, short_hit, "short_hit_x100");
	RUN(op_short_miss, short_miss, "short_miss_x100");
	RUN(op_anchor, anchor_in, "anchor_x100");

	drift_string_release(carrier64);
	drift_string_release(carrier2m);
	drift_string_release(nomatch64);
	drift_string_release(nomatch2m);
	drift_string_release(zw);
	drift_string_release(short_hit);
	drift_string_release(short_miss);
	drift_string_release(anchor_in);
	printf("DONE\n");
	return 0;
}
