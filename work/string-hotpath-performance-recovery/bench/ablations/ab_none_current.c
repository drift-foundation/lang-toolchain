// Drift String runtime support (lang, ABI 22 — B-repr/B5 RcBytes).
#include "string_runtime.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdatomic.h>
#include <stddef.h>
#include <execinfo.h>
#include <pthread.h>
#include <unistd.h>

#include "ryu/ryu.h"

/* ── Contract failure (unconditional, both runtime variants) ─────── */

_Noreturn void drift_contract_fail(const char *what) {
	fprintf(stderr, "[drift:contract] %s\n", what ? what : "(null)");
	fflush(stderr);
	abort();
}

/* ── The canonical empty singleton (ABI-22 symbol) ────────────────── */

__attribute__((visibility("hidden")))
const struct DriftEmptyString __drift_rt_string_empty = {
	{ 1, DRIFT_RCBYTES_IMMORTAL | DRIFT_RCBYTES_NUL_SCANNED },
	{ 0 } };

DriftString drift_string_empty(void) {
	DriftString s = {0, (DriftRcBytes *)&__drift_rt_string_empty.hdr};
	return s;
}

/* ── Handle validation (shared prologue, ALL builds) ──────────────────
 * Exactly {0, NULL} is the drop-only TOMBSTONE (the compiler's
 * zero-storage doctrine writes it; release-family accepts it as a
 * no-op, observation-family fails closed).  Everything else malformed
 * fails closed everywhere.  Illegal flag combinations and reserved
 * bits go through the UNCONDITIONAL contract path — never
 * NDEBUG-gated. */

typedef struct DriftStringCheck {
	enum { DRIFT_STR_TOMBSTONE, DRIFT_STR_LIVE } state;
	uint64_t flags;
} DriftStringCheck;

/* drift-owned-string-audit: allow read-only-borrow -- s
 * Pure validation prologue: inspects the handle words and the flags
 * word; never touches refcounts or ownership. */
static DriftStringCheck drift_string_validate(DriftString s) {
	DriftStringCheck chk;
	if (s.storage == NULL) {
		if (s.len != 0) {
			drift_contract_fail("malformed String handle: nonzero len, NULL storage");
		}
		chk.state = DRIFT_STR_TOMBSTONE;
		chk.flags = 0;
		return chk;
	}
	if (s.len < 0) {
		drift_contract_fail("malformed String handle: negative len");
	}
	uint64_t f = atomic_load_explicit(&s.storage->flags, memory_order_relaxed);
	if (f & DRIFT_RCBYTES_RESERVED_MASK) {
		drift_contract_fail("String flags: reserved bit set");
	}
	if ((f & (DRIFT_RCBYTES_STATIC | DRIFT_RCBYTES_IMMORTAL))
			== (DRIFT_RCBYTES_STATIC | DRIFT_RCBYTES_IMMORTAL)) {
		drift_contract_fail("String flags: STATIC+IMMORTAL");
	}
	if ((f & (DRIFT_RCBYTES_NUL_SCANNED | DRIFT_RCBYTES_HAS_INTERIOR_NUL))
			== DRIFT_RCBYTES_HAS_INTERIOR_NUL) {
		drift_contract_fail("String flags: HAS_INTERIOR_NUL without NUL_SCANNED");
	}
	chk.state = DRIFT_STR_LIVE;
	chk.flags = f;
	return chk;
}

/* Live-handle bytes base — layout authority, this file only. */
static unsigned char *drift_string_bytes_mut(DriftString s) {
	return (unsigned char *)(s.storage + 1);
}

/* DRIFT_STR_TRACE: env-gated diagnostic for refcount-race investigations.
 * Logs every retain/release on heap-allocated (non-static) DriftStrings
 * to stderr with: header ptr, refcount transition, len, first 32 bytes
 * of content, thread id, and a 6-frame backtrace.
 *
 * Set DRIFT_STR_TRACE=1 to enable.  Optional DRIFT_STR_TRACE_FILTER=<substr>
 * narrows to events whose String content contains the substring (case-
 * sensitive prefix match on the first 32 bytes); useful for isolating
 * a specific allocation (e.g. "memcheck-secret") from the broader noise
 * of an app run.  Filtering is per-event; the alloc-time trace at
 * drift_string_alloc / drift_string_from_cstr always fires unfiltered
 * (so you see the original allocation that anchors all later events).
 *
 * Zero cost when the env var is unset (one getenv per call, all sites
 * short-circuit before any formatting).  Investigation aid; not for
 * production use. */
static void drift_str_trace_event(const char *what, void *hdr,
		const char *data, long len, uint64_t prev_rc, uint64_t new_rc) {
	const char *want = getenv("DRIFT_STR_TRACE_FILTER");
	if (want && *want) {
		if (!data || len <= 0) return;
		size_t wlen = strlen(want);
		size_t scan = (size_t)len < 32 ? (size_t)len : 32;
		if (wlen > scan) return;
		int hit = 0;
		for (size_t i = 0; i + wlen <= scan; i++) {
			if (memcmp(data + i, want, wlen) == 0) { hit = 1; break; }
		}
		if (!hit) return;
	}
	fprintf(stderr, "[str] %s ptr=%p rc=%lu->%lu len=%ld tid=%lu val=\"%.32s\"\n",
		what, hdr, (unsigned long)prev_rc, (unsigned long)new_rc,
		len, (unsigned long)pthread_self(), data ? data : "");
	void *bt[7];
	int n = backtrace(bt, 7);
	/* Skip frame 0 (this helper) and frame 1 (the caller's prologue
	 * stub if any) so the first printed frame is the actual call site. */
	if (n > 1) backtrace_symbols_fd(bt + 1, n - 1, STDERR_FILENO);
	fflush(stderr);
}

/* ── Allocation (§2.5: every invalid input fails closed, never
 *    silently empty) ──────────────────────────────────────────────── */

static DriftRcBytes *drift_string_alloc_block(drift_isize len) {
	if (len < 0) {
		drift_contract_fail("negative String length");
	}
	if (len > DRIFT_STRING_MAX_LEN) {
		drift_contract_fail("String length overflow");
	}
	size_t total = sizeof(DriftRcBytes) + (size_t)len + 1;
	DriftRcBytes *blk = (DriftRcBytes *)malloc(total);
	if (!blk) {
		abort();
	}
	atomic_store_explicit(&blk->strong, 1, memory_order_relaxed);
	atomic_store_explicit(&blk->flags, 0, memory_order_relaxed);
	((unsigned char *)(blk + 1))[len] = 0;
	return blk;
}

/* Shared copying constructor body: data must be non-NULL when len > 0;
 * len == 0 canonicalizes to the empty singleton. */
static DriftString drift_string_new_copy(const char *data, drift_isize len, const char *null_what) {
	if (len == 0) {
		return drift_string_empty();
	}
	if (data == NULL) {
		drift_contract_fail(null_what);
	}
	DriftRcBytes *blk = drift_string_alloc_block(len);
	memcpy(blk + 1, data, (size_t)len);
	DriftString s = {len, blk};
	return s;
}

DriftString drift_string_from_cstr(const char *cstr) {
	if (cstr == NULL) {
		drift_contract_fail("NULL cstr");
	}
	size_t n = strlen(cstr);
	if (n > (size_t)DRIFT_STRING_MAX_LEN) {
		/* validate BEFORE the size_t -> drift_isize conversion */
		drift_contract_fail("String length overflow");
	}
	return drift_string_new_copy(cstr, (drift_isize)n, "NULL cstr");
}

DriftString drift_string_from_utf8_bytes(const char *data, drift_isize len) {
	if (len < 0) {
		drift_contract_fail("negative String length");
	}
	return drift_string_new_copy(data, len, "NULL bytes with nonzero length");
}

DriftString drift_string_from_int64(int64_t v) {
	/* worst-case length for int64_t in decimal, including sign */
	char buf[32];
	int n = snprintf(buf, sizeof(buf), "%lld", (long long)v);
	if (n < 0) {
		abort();
	}
	return drift_string_from_utf8_bytes(buf, (drift_isize)n);
}

DriftString drift_string_from_uint64(uint64_t v) {
	/* worst-case length for uint64_t in decimal */
	char buf[32];
	int n = snprintf(buf, sizeof(buf), "%llu", (unsigned long long)v);
	if (n < 0) {
		abort();
	}
	return drift_string_from_utf8_bytes(buf, (drift_isize)n);
}

DriftString drift_string_from_f64(double v) {
	/*
	Deterministic `Float` formatting using Ryu.

	We vendor Ryu into lang so we can format floats without relying on libc's
	`snprintf` behavior (locale, rounding mode, and formatting edge cases differ
	across platforms/libcs).

	Ryu guarantees a shortest-roundtrip decimal representation.
	*/
	char buf[64];
	int n = d2s_buffered_n(v, buf);
	if (n <= 0) {
		abort();
	}
	return drift_string_from_utf8_bytes(buf, (drift_isize)n);
}

DriftString drift_string_from_bool(int v) {
	/* Runtime-owned immortal constants: IMMORTAL (not STATIC — mutual
	 * exclusion; STATIC is the compiler-rodata population) and fully
	 * NUL-scanned at construction. */
	static const struct { DriftRcBytes hdr; unsigned char data[5]; } k_true = {
		{ 1, DRIFT_RCBYTES_IMMORTAL | DRIFT_RCBYTES_NUL_SCANNED },
		"true",
	};
	static const struct { DriftRcBytes hdr; unsigned char data[6]; } k_false = {
		{ 1, DRIFT_RCBYTES_IMMORTAL | DRIFT_RCBYTES_NUL_SCANNED },
		"false",
	};
	if (v) {
		DriftString s = {4, (DriftRcBytes *)&k_true.hdr};
		return s;
	}
	DriftString s = {5, (DriftRcBytes *)&k_false.hdr};
	return s;
}

DriftString drift_string_literal(const char *data, drift_isize len) {
	if (len < 0) {
		drift_contract_fail("negative String length");
	}
	return drift_string_new_copy(data, len, "NULL literal bytes with nonzero length");
}

/* drift-owned-string-audit: allow read-only-borrow -- a, b
 * Reads a and b through the layout-authority bytes base to build a
 * fresh allocation.  Does NOT release a or b; the originating Drift IR
 * emits explicit `drift_string_release(a); drift_string_release(b);`
 * after the concat call to drop the input stakes. */
DriftString drift_string_concat(DriftString a, DriftString b) {
	DriftStringCheck ca = drift_string_validate(a);
	DriftStringCheck cb = drift_string_validate(b);
	if (ca.state == DRIFT_STR_TOMBSTONE || cb.state == DRIFT_STR_TOMBSTONE) {
		drift_contract_fail("String tombstone observed (concat)");
	}
	if (b.len > DRIFT_STRING_MAX_LEN - a.len) {
		/* subtraction form cannot wrap: a.len <= MAX_LEN is invariant */
		drift_contract_fail("String concat overflow");
	}
	drift_isize total = a.len + b.len;
	if (total == 0) {
		return drift_string_empty();
	}
	DriftRcBytes *blk = drift_string_alloc_block(total);
	unsigned char *out = (unsigned char *)(blk + 1);
	if (a.len > 0) {
		memcpy(out, drift_string_bytes_mut(a), (size_t)a.len);
	}
	if (b.len > 0) {
		memcpy(out + a.len, drift_string_bytes_mut(b), (size_t)b.len);
	}
	DriftString s = {total, blk};
	return s;
}

/* drift-owned-string-audit: allow refcount-primitive -- s
 * This IS the retain primitive; bumps the refcount and returns the
 * same handle.  Deliberately keeps the caller's stake AND adds one
 * for the returned value.  Tombstone retain FAILS CLOSED (§2.4 —
 * subject to the armed-trap reachability gate). */
DriftString drift_string_retain(DriftString s) {
	DriftStringCheck chk = drift_string_validate(s);
	if (chk.state == DRIFT_STR_TOMBSTONE) {
		drift_contract_fail("retain of String tombstone");
	}
	if (chk.flags & (DRIFT_RCBYTES_STATIC | DRIFT_RCBYTES_IMMORTAL)) {
		return s;
	}
	uint64_t prev = atomic_fetch_add_explicit(&s.storage->strong, 1, memory_order_relaxed);
	if (prev >= DRIFT_RC_MAX_LIVE) {
		/* unconditional fail-closed, normal AND NDEBUG builds; fires
		 * ~2^63 increments before any wrap (>= not >) */
		drift_contract_fail("String refcount overflow");
	}
	/* ablation: trace branch removed */
	return s;
}

/* drift-owned-string-audit: allow refcount-primitive -- s
 * This IS the release primitive; do NOT wrap in DRIFT_OWNED_STRING
 * (would recurse on every call).  The exact all-zero tombstone is a
 * drop-only NO-OP (zero-storage drop safety); malformed handles fail
 * closed even here. */
void drift_string_release(DriftString s) {
	DriftStringCheck chk = drift_string_validate(s);
	if (chk.state == DRIFT_STR_TOMBSTONE) {
		return;
	}
	if (chk.flags & (DRIFT_RCBYTES_STATIC | DRIFT_RCBYTES_IMMORTAL)) {
		return;
	}
	uint64_t prev = atomic_fetch_sub_explicit(&s.storage->strong, 1, memory_order_release);
	if (prev == 0) {
		abort(); /* underflow — unconditional, as B0 */
	}
	/* ablation: trace branch removed */
	if (prev == 1) {
		atomic_thread_fence(memory_order_acquire);
		free(s.storage);
	}
}

/* drift-owned-string-audit: allow refcount-primitive -- s
 * Thin alias for drift_string_release (same release semantics). */
void drift_string_free(DriftString s) {
	drift_string_release(s);
}

/* drift-owned-string-audit: allow read-only-borrow -- s
 * Pure read: copies the bytes into a freshly malloc'd cstring.
 * Does NOT release; caller retains the stake (typically released
 * after the returned cstring is consumed).  RETAINED ABI-21 semantics
 * (decision 7): allocating/owned, name unchanged. */
char *drift_string_to_cstr(DriftString s) {
	DriftStringCheck chk = drift_string_validate(s);
	if (chk.state == DRIFT_STR_TOMBSTONE) {
		drift_contract_fail("String tombstone observed (to_cstr)");
	}
	size_t len = (size_t)s.len;
	char *buf = (char *)malloc(len + 1);
	if (!buf) {
		abort();
	}
	if (len > 0) {
		memcpy(buf, drift_string_bytes_mut(s), len);
	}
	buf[len] = '\0';
	return buf;
}

/* drift-owned-string-audit: allow read-only-borrow -- a, b
 * Pure read: compares bytes; does not touch refcounts. */
int drift_string_eq(DriftString a, DriftString b) {
	DriftStringCheck ca = drift_string_validate(a);
	DriftStringCheck cb = drift_string_validate(b);
	if (ca.state == DRIFT_STR_TOMBSTONE || cb.state == DRIFT_STR_TOMBSTONE) {
		drift_contract_fail("String tombstone observed (eq)");
	}
	if (a.len != b.len) {
		return 0;
	}
	if (a.len == 0) {
		return 1;
	}
	return memcmp(drift_string_bytes_mut(a), drift_string_bytes_mut(b), (size_t)a.len) == 0;
}

/* drift-owned-string-audit: allow read-only-borrow -- a, b
 * Pure read: byte-lex compare; does not touch refcounts. */
int drift_string_cmp(DriftString a, DriftString b) {
	DriftStringCheck ca = drift_string_validate(a);
	DriftStringCheck cb = drift_string_validate(b);
	if (ca.state == DRIFT_STR_TOMBSTONE || cb.state == DRIFT_STR_TOMBSTONE) {
		drift_contract_fail("String tombstone observed (cmp)");
	}
	const size_t a_len = (size_t)a.len;
	const size_t b_len = (size_t)b.len;
	const size_t min_len = a_len < b_len ? a_len : b_len;

	if (min_len > 0) {
		// memcmp uses unsigned byte ordering; this matches our spec for
		// `String` comparison operators.
		const int cmp = memcmp(drift_string_bytes_mut(a), drift_string_bytes_mut(b), min_len);
		if (cmp != 0) {
			return cmp;
		}
	}

	// Shared prefix is equal; shorter string sorts first.
	if (a_len < b_len) {
		return -1;
	}
	if (a_len > b_len) {
		return 1;
	}
	return 0;
}

/* ── C-string bridge helpers (§3.3) ──────────────────────────────── */

/* drift-owned-string-audit: allow read-only-borrow -- s (by pointer)
 * Owns the NUL-cache protocol: EXISTENCE is cached monotonically in
 * the flags (one relaxed fetch_or from the unknown state; concurrent
 * scanners race benignly to identical bits); the exact INDEX is
 * re-scanned on the error path. */
drift_isize drift_string_interior_nul_index(const DriftString *s) {
	DriftStringCheck chk = drift_string_validate(*s);
	if (chk.state == DRIFT_STR_TOMBSTONE) {
		drift_contract_fail("String tombstone observed (interior_nul_index)");
	}
	if (s->len == 0) {
		return -1;
	}
	const unsigned char *bytes = (const unsigned char *)(s->storage + 1);
	if (chk.flags & DRIFT_RCBYTES_NUL_SCANNED) {
		if (!(chk.flags & DRIFT_RCBYTES_HAS_INTERIOR_NUL)) {
			return -1; /* zero-copy fast path: scanned, clean */
		}
		const unsigned char *hit = memchr(bytes, 0, (size_t)s->len);
		if (hit == NULL) {
			drift_contract_fail("String NUL cache: HAS_INTERIOR_NUL but no NUL found");
		}
		return (drift_isize)(hit - bytes);
	}
	const unsigned char *hit = memchr(bytes, 0, (size_t)s->len);
	uint64_t publish = DRIFT_RCBYTES_NUL_SCANNED
		| (hit ? DRIFT_RCBYTES_HAS_INTERIOR_NUL : 0);
	atomic_fetch_or_explicit(&s->storage->flags, publish, memory_order_relaxed);
	return hit ? (drift_isize)(hit - bytes) : -1;
}

char *drift_string_to_owned_cstr(const DriftString *s, drift_isize *nul_index_out) {
	drift_isize nul = drift_string_interior_nul_index(s);
	if (nul >= 0) {
		if (nul_index_out) {
			*nul_index_out = nul;
		}
		return NULL;
	}
	if (nul_index_out) {
		*nul_index_out = -1;
	}
	size_t len = (size_t)s->len;
	char *buf = (char *)malloc(len + 1);
	if (!buf) {
		abort();
	}
	if (len > 0) {
		memcpy(buf, s->storage + 1, len);
	}
	buf[len] = '\0';
	return buf;
}

char *drift_string_to_owned_cstr_unchecked(const DriftString *s) {
	DriftStringCheck chk = drift_string_validate(*s);
	if (chk.state == DRIFT_STR_TOMBSTONE) {
		drift_contract_fail("String tombstone observed (to_owned_cstr_unchecked)");
	}
	size_t len = (size_t)s->len;
	char *buf = (char *)malloc(len + 1);
	if (!buf) {
		abort();
	}
	if (len > 0) {
		memcpy(buf, s->storage + 1, len);
	}
	buf[len] = '\0';
	return buf;
}

void drift_cstr_free(char *p) {
	free(p);
}

DriftCBytes drift_string_to_owned_cbytes(const DriftString *s) {
	DriftStringCheck chk = drift_string_validate(*s);
	if (chk.state == DRIFT_STR_TOMBSTONE) {
		drift_contract_fail("String tombstone observed (to_owned_cbytes)");
	}
	size_t len = (size_t)s->len;
	unsigned char *buf = (unsigned char *)malloc(len + 1);
	if (!buf) {
		abort();
	}
	if (len > 0) {
		memcpy(buf, s->storage + 1, len);
	}
	buf[len] = 0;
	DriftCBytes out = {buf, s->len};
	return out;
}

void drift_cbytes_free(DriftCBytes b) {
	free(b.ptr);
}
