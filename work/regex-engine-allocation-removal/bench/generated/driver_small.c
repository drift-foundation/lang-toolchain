/* regex-engine-allocation-removal: GENERATED small-suite count driver (same shim as driver.c).
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


extern DriftString smk_alt_256(void);
extern DriftString smk_alt_4096(void);
extern DriftString smk_early_256(void);
extern DriftString smk_early_4096(void);
extern DriftString smk_fill_1024(void);
extern DriftString smk_fill_128(void);
extern DriftString smk_fill_256(void);
extern DriftString smk_fill_4096(void);
extern DriftString smk_fill_512(void);
extern DriftString smk_fill_64(void);
extern DriftString smk_late_1024(void);
extern DriftString smk_late_128(void);
extern DriftString smk_late_256(void);
extern DriftString smk_late_4096(void);
extern DriftString smk_late_512(void);
extern DriftString smk_late_64(void);

extern long sc_late_64(DriftString);
extern long sc_late_128(DriftString);
extern long sc_late_256(DriftString);
extern long sc_late_512(DriftString);
extern long sc_late_1024(DriftString);
extern long sc_late_4096(DriftString);
extern long sc_nomatch_64(DriftString);
extern long sc_nomatch_128(DriftString);
extern long sc_nomatch_256(DriftString);
extern long sc_nomatch_512(DriftString);
extern long sc_nomatch_1024(DriftString);
extern long sc_nomatch_4096(DriftString);
extern long sc_early_256(DriftString);
extern long sc_early_4096(DriftString);
extern long sc_anchored_256(DriftString);
extern long sc_anchored_4096(DriftString);
extern long sc_alt_256(DriftString);
extern long sc_alt_4096(DriftString);
extern long sc_late_view_256(DriftString);
extern long sc_late_view_4096(DriftString);
extern long sc_nomatch_view_256(DriftString);
extern long sc_nomatch_view_4096(DriftString);
extern long sc_compile_p1(DriftString);
extern long sc_compile_pa(DriftString);
extern long sc_compile_palt(DriftString);

#define RUN(fn, subj, label) do { \
	DriftString a = drift_string_retain(subj); \
	reset(); \
	long r = fn(a); \
	report(label, r); \
	if (r < 0) { printf("OPFAIL=%s r=%ld\n", label, r); return 70; } \
} while (0)

int main(void) {
	DriftString subj_smk_alt_256 = smk_alt_256();
	DriftString subj_smk_alt_4096 = smk_alt_4096();
	DriftString subj_smk_early_256 = smk_early_256();
	DriftString subj_smk_early_4096 = smk_early_4096();
	DriftString subj_smk_fill_1024 = smk_fill_1024();
	DriftString subj_smk_fill_128 = smk_fill_128();
	DriftString subj_smk_fill_256 = smk_fill_256();
	DriftString subj_smk_fill_4096 = smk_fill_4096();
	DriftString subj_smk_fill_512 = smk_fill_512();
	DriftString subj_smk_fill_64 = smk_fill_64();
	DriftString subj_smk_late_1024 = smk_late_1024();
	DriftString subj_smk_late_128 = smk_late_128();
	DriftString subj_smk_late_256 = smk_late_256();
	DriftString subj_smk_late_4096 = smk_late_4096();
	DriftString subj_smk_late_512 = smk_late_512();
	DriftString subj_smk_late_64 = smk_late_64();

	RUN(sc_compile_p1, subj_smk_alt_256, "sc_compile_p1");
	RUN(sc_compile_pa, subj_smk_alt_256, "sc_compile_pa");
	RUN(sc_compile_palt, subj_smk_alt_256, "sc_compile_palt");
	RUN(sc_late_64, subj_smk_late_64, "sc_late_64");
	RUN(sc_late_128, subj_smk_late_128, "sc_late_128");
	RUN(sc_late_256, subj_smk_late_256, "sc_late_256");
	RUN(sc_late_512, subj_smk_late_512, "sc_late_512");
	RUN(sc_late_1024, subj_smk_late_1024, "sc_late_1024");
	RUN(sc_late_4096, subj_smk_late_4096, "sc_late_4096");
	RUN(sc_nomatch_64, subj_smk_fill_64, "sc_nomatch_64");
	RUN(sc_nomatch_128, subj_smk_fill_128, "sc_nomatch_128");
	RUN(sc_nomatch_256, subj_smk_fill_256, "sc_nomatch_256");
	RUN(sc_nomatch_512, subj_smk_fill_512, "sc_nomatch_512");
	RUN(sc_nomatch_1024, subj_smk_fill_1024, "sc_nomatch_1024");
	RUN(sc_nomatch_4096, subj_smk_fill_4096, "sc_nomatch_4096");
	RUN(sc_early_256, subj_smk_early_256, "sc_early_256");
	RUN(sc_early_4096, subj_smk_early_4096, "sc_early_4096");
	RUN(sc_anchored_256, subj_smk_fill_256, "sc_anchored_256");
	RUN(sc_anchored_4096, subj_smk_fill_4096, "sc_anchored_4096");
	RUN(sc_alt_256, subj_smk_alt_256, "sc_alt_256");
	RUN(sc_alt_4096, subj_smk_alt_4096, "sc_alt_4096");
	RUN(sc_late_view_256, subj_smk_late_256, "sc_late_view_256");
	RUN(sc_late_view_4096, subj_smk_late_4096, "sc_late_view_4096");
	RUN(sc_nomatch_view_256, subj_smk_fill_256, "sc_nomatch_view_256");
	RUN(sc_nomatch_view_4096, subj_smk_fill_4096, "sc_nomatch_view_4096");

	drift_string_release(subj_smk_alt_256);
	drift_string_release(subj_smk_alt_4096);
	drift_string_release(subj_smk_early_256);
	drift_string_release(subj_smk_early_4096);
	drift_string_release(subj_smk_fill_1024);
	drift_string_release(subj_smk_fill_128);
	drift_string_release(subj_smk_fill_256);
	drift_string_release(subj_smk_fill_4096);
	drift_string_release(subj_smk_fill_512);
	drift_string_release(subj_smk_fill_64);
	drift_string_release(subj_smk_late_1024);
	drift_string_release(subj_smk_late_128);
	drift_string_release(subj_smk_late_256);
	drift_string_release(subj_smk_late_4096);
	drift_string_release(subj_smk_late_512);
	drift_string_release(subj_smk_late_64);
	printf("DONE\n");
	return 0;
}
