// Drift String runtime support (lang, v1).
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

typedef struct DriftStringHeader {
	_Atomic uint64_t refcount;
	uint64_t flags;
} DriftStringHeader;

enum {
	DRIFT_STRING_FLAG_STATIC = 1ULL << 0,
};

_Static_assert(sizeof(DriftStringHeader) == 16, "DriftStringHeader layout must stay stable");
_Static_assert(offsetof(DriftStringHeader, flags) == 8, "DriftStringHeader flags offset must stay stable");
_Static_assert(DRIFT_STRING_FLAG_STATIC == 1ULL, "DriftStringHeader static flag must stay stable");


static DriftStringHeader *drift_string_header(char *data) {
	return (DriftStringHeader *)(data - sizeof(DriftStringHeader));
}

static char *drift_string_alloc(drift_isize len) {
	if (len < 0) {
		return NULL;
	}
	size_t total = sizeof(DriftStringHeader) + (size_t)len + 1;
	DriftStringHeader *hdr = (DriftStringHeader *)malloc(total);
	if (!hdr) {
		abort();
	}
	hdr->refcount = 1;
	hdr->flags = 0;
	return (char *)(hdr + 1);
}

DriftString drift_string_from_cstr(const char *cstr) {
	if (cstr == NULL) {
		DriftString s = {0, NULL};
		return s;
	}
	drift_isize len = (drift_isize)strlen(cstr);
	char *buf = drift_string_alloc(len);
	if (!buf) {
		DriftString s = {0, NULL};
		return s;
	}
	memcpy(buf, cstr, (size_t)len);
	buf[len] = '\0';
	DriftString s = {len, buf};
	return s;
}

DriftString drift_string_from_utf8_bytes(const char *data, drift_isize len) {
	if (data == NULL || len == 0) {
		DriftString s = {0, NULL};
		return s;
	}
	char *buf = drift_string_alloc(len);
	if (!buf) {
		DriftString s = {0, NULL};
		return s;
	}
	memcpy(buf, data, (size_t)len);
	buf[len] = '\0';
	DriftString s = {len, buf};
	return s;
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
	static struct {
		DriftStringHeader hdr;
		char data[5];
	} k_true = {
		.hdr = {.refcount = 1, .flags = DRIFT_STRING_FLAG_STATIC},
		.data = "true",
	};
	static struct {
		DriftStringHeader hdr;
		char data[6];
	} k_false = {
		.hdr = {.refcount = 1, .flags = DRIFT_STRING_FLAG_STATIC},
		.data = "false",
	};
	if (v) {
		DriftString s = {4, k_true.data};
		return s;
	}
	DriftString s = {5, k_false.data};
	return s;
}

DriftString drift_string_literal(const char *data, drift_isize len) {
	if (data == NULL || len == 0) {
		DriftString s = {0, NULL};
		return s;
	}
	char *buf = drift_string_alloc(len);
	if (!buf) {
		DriftString s = {0, NULL};
		return s;
	}
	memcpy(buf, data, (size_t)len);
	buf[len] = '\0';
	DriftString s = {len, buf};
	return s;
}

/* drift-owned-string-audit: allow read-only-borrow -- a, b
 * Reads a.len/a.data and b.len/b.data to build a fresh allocation.
 * Does NOT release a or b; the originating Drift IR emits explicit
 * `drift_string_release(a); drift_string_release(b);` after the
 * concat call to drop the input stakes. */
DriftString drift_string_concat(DriftString a, DriftString b) {
	if ((size_t)-1 - (size_t)a.len < (size_t)b.len) {
		abort();
	}
	drift_isize total = a.len + b.len;
	/* For empty result, canonicalize to len=0, data=NULL to avoid heap allocs. */
	if (total == 0) {
		DriftString s = {0, NULL};
		return s;
	}
	char *buf = drift_string_alloc(total);
	if (!buf) {
		DriftString s = {0, NULL};
		return s;
	}
	if (a.len > 0 && a.data) {
		memcpy(buf, a.data, (size_t)a.len);
	}
	if (b.len > 0 && b.data) {
		memcpy(buf + a.len, b.data, (size_t)b.len);
	}
	buf[total] = '\0';
	DriftString s = {total, buf};
	return s;
}

/* drift-owned-string-audit: allow refcount-primitive -- s
 * This IS the retain primitive; bumps the refcount and returns the
 * same handle.  Deliberately keeps the caller's stake AND adds one
 * for the returned value. */
DriftString drift_string_retain(DriftString s) {
	if (s.data == NULL) {
		return s;
	}
	DriftStringHeader *hdr = drift_string_header(s.data);
	if (hdr->flags & DRIFT_STRING_FLAG_STATIC) {
		return s;
	}
	uint64_t prev = atomic_fetch_add_explicit(&hdr->refcount, 1, memory_order_relaxed);
	if (getenv("DRIFT_STR_TRACE")) {
		drift_str_trace_event("retain", hdr, s.data, (long)s.len, prev, prev + 1);
	}
	return s;
}

/* drift-owned-string-audit: allow refcount-primitive -- s
 * This IS the release primitive; do NOT wrap in DRIFT_OWNED_STRING
 * (would recurse on every call). */
void drift_string_release(DriftString s) {
	if (s.data == NULL) {
		return;
	}
	DriftStringHeader *hdr = drift_string_header(s.data);
	if (hdr->flags & DRIFT_STRING_FLAG_STATIC) {
		return;
	}
	uint64_t prev = atomic_fetch_sub_explicit(&hdr->refcount, 1, memory_order_release);
	if (prev == 0) {
		abort();
	}
	if (prev == 1) {
		atomic_thread_fence(memory_order_acquire);
#ifndef NDEBUG
		if (hdr->flags & DRIFT_STRING_FLAG_STATIC) {
			abort();
		}
#endif
		free(hdr);
	}
}

/* drift-owned-string-audit: allow refcount-primitive -- s
 * Thin alias for drift_string_release (same release semantics). */
void drift_string_free(DriftString s) {
	drift_string_release(s);
}

/* drift-owned-string-audit: allow read-only-borrow -- s
 * Pure read: copies s.data bytes into a freshly malloc'd cstring.
 * Does NOT release; caller retains the stake (typically released
 * after the returned cstring is consumed). */
char *drift_string_to_cstr(DriftString s) {
	size_t len = (size_t)s.len;
	char *buf = (char *)malloc(len + 1);
	if (!buf) {
		abort();
	}
	if (s.data && s.len > 0) {
		memcpy(buf, s.data, len);
	}
	buf[len] = '\0';
	return buf;
}

/* drift-owned-string-audit: allow read-only-borrow -- a, b
 * Pure read: compares bytes; does not touch refcounts. */
int drift_string_eq(DriftString a, DriftString b) {
	if (a.len != b.len) {
		return 0;
	}
	if (a.len == 0) {
		return 1;
	}
	if (!a.data || !b.data) {
		return 0;
	}
	return memcmp(a.data, b.data, (size_t)a.len) == 0;
}

/* drift-owned-string-audit: allow read-only-borrow -- a, b
 * Pure read: byte-lex compare; does not touch refcounts. */
int drift_string_cmp(DriftString a, DriftString b) {
	const size_t a_len = (size_t)a.len;
	const size_t b_len = (size_t)b.len;
	const size_t min_len = a_len < b_len ? a_len : b_len;

	if (min_len > 0) {
		// memcmp uses unsigned byte ordering; this matches our spec for
		// `String` comparison operators.
		const int cmp = memcmp(a.data, b.data, min_len);
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
