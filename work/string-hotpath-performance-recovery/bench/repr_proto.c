/* string-hotpath-performance-recovery: representation-design
 * head-to-head at the primitive level (C prototypes, carrier token
 * mix from the measured histogram).
 *
 * R0 control  — ABI-22-shaped branch-lean (header flags load +
 *               combined invariant test; env cached), the measured
 *               ab3 shape.
 * R1 tagged   — heap/static/immortal kind in aligned pointer low
 *               bits; retain/release touch NO memory for non-heap;
 *               heap ops load NO flags word (refcount only);
 *               malformed handles -> cold path via tag decode.
 * R2 sso16    — 16-byte handle, <=15 bytes inline (tag byte packs
 *               kind+len); inline materialize = copy only, drop =
 *               nothing; heap fallback shaped like R0.
 * R3 sso+tag  — R2 inline + R1 tagged heap fallback.
 *
 * Caveats (stated in the checkpoint): prototypes measure the
 * primitive-op cost floor of each representation on identical data;
 * they cannot capture Drift codegen effects (by-value ABI passing of
 * a 16-byte struct, register pressure, observation-guard interplay).
 * Rankings, not absolute promises.
 */
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static uint64_t now_ns(void) {
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

/* carrier token mix (parse histogram): 9 tokens per request */
static const int TOKEN_LENS[9] = {3, 7, 7, 1, 4, 9, 6, 16, 10};
static const char SRC[128] =
	"GET /health?verbose=1 HTTP/1.1 Host: localhost Accept: application/json User-Agent: pin/1.0 xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx";

/* ---------------- shared heap block (ABI-22 RcBytes shape) -------- */
typedef struct Blk {
	_Atomic uint64_t strong;
	_Atomic uint64_t flags;
} Blk;

/* ============ R0: branch-lean ABI-22 shape (control) ============ */
typedef struct S0 {
	int64_t len;
	Blk *storage;
} S0;

__attribute__((noinline, cold)) static void fail0(void) { abort(); }

static inline uint64_t validate0(S0 s) {
	uint64_t f = atomic_load_explicit(&s.storage->flags, memory_order_relaxed);
	uint64_t bad = (uint64_t)(s.len < 0) | (f & 0xFFF0ull);
	if (__builtin_expect(bad != 0, 0)) fail0();
	return f;
}

static S0 s0_make(const char *p, int64_t len) {
	Blk *b = malloc(sizeof(Blk) + len + 1);
	atomic_store_explicit(&b->strong, 1, memory_order_relaxed);
	atomic_store_explicit(&b->flags, 0, memory_order_relaxed);
	memcpy(b + 1, p, len);
	((char *)(b + 1))[len] = 0;
	return (S0){len, b};
}

static void s0_release(S0 s) {
	if (!s.storage) return;
	uint64_t f = validate0(s);
	if (f & 3ull) return; /* static|immortal */
	uint64_t prev = atomic_fetch_sub_explicit(&s.storage->strong, 1, memory_order_release);
	if (prev == 1) {
		atomic_thread_fence(memory_order_acquire);
		free(s.storage);
	}
}

static S0 s0_retain(S0 s) {
	if (!s.storage) return s;
	uint64_t f = validate0(s);
	if (f & 3ull) return s;
	atomic_fetch_add_explicit(&s.storage->strong, 1, memory_order_relaxed);
	return s;
}

static int s0_eq(S0 a, S0 b) {
	if (a.len != b.len) return 0;
	validate0(a); validate0(b);
	if (a.len == 0) return 1;
	return memcmp(a.storage + 1, b.storage + 1, a.len) == 0;
}

/* ============ R1: tagged pointer bits ============ */
typedef struct S1 {
	int64_t len;
	uintptr_t p; /* low 2 bits: 0=heap, 1=static, 2=immortal; 0/NULL=tombstone */
} S1;

static S1 s1_make(const char *p, int64_t len) {
	Blk *b = malloc(sizeof(Blk) + len + 1);
	atomic_store_explicit(&b->strong, 1, memory_order_relaxed);
	atomic_store_explicit(&b->flags, 0, memory_order_relaxed);
	memcpy(b + 1, p, len);
	((char *)(b + 1))[len] = 0;
	return (S1){len, (uintptr_t)b};
}

static void s1_release(S1 s) {
	if ((s.p & 3) || s.p == 0) return; /* static/immortal/tombstone: no memory touch */
	Blk *b = (Blk *)s.p;
	uint64_t prev = atomic_fetch_sub_explicit(&b->strong, 1, memory_order_release);
	if (prev == 1) {
		atomic_thread_fence(memory_order_acquire);
		free(b);
	}
}

static S1 s1_retain(S1 s) {
	if ((s.p & 3) || s.p == 0) return s;
	atomic_fetch_add_explicit(&((Blk *)s.p)->strong, 1, memory_order_relaxed);
	return s;
}

static const char *s1_bytes(S1 s) {
	return (const char *)(((Blk *)(s.p & ~3ull)) + 1);
}

static int s1_eq(S1 a, S1 b) {
	if (a.len != b.len) return 0;
	if (a.len == 0) return 1;
	if (a.p == b.p) return 1; /* pointer-identity fast path */
	return memcmp(s1_bytes(a), s1_bytes(b), a.len) == 0;
}

/* ============ R2: SSO-16 (<=15 inline), heap like R0 ============
 * Layout: 16 raw bytes.  raw[15] (the heap pointer's most-significant
 * byte, always 0x00 for canonical x86-64 user pointers) is the tag:
 * 0x00 -> heap {int64 len, Blk* storage}; 0x80|len -> inline, bytes
 * in raw[0..14]. */
typedef union S2 {
	struct { int64_t len; Blk *storage; } h;
	unsigned char raw[16];
} S2;

_Static_assert(sizeof(S2) == 16, "SSO handle must stay 16 bytes");

static S2 s2_make(const char *p, int64_t len) {
	S2 s;
	if (len <= 15) {
		memcpy(s.raw, p, len);
		s.raw[15] = (unsigned char)(0x80 | len);
		return s;
	}
	Blk *b = malloc(sizeof(Blk) + len + 1);
	atomic_store_explicit(&b->strong, 1, memory_order_relaxed);
	atomic_store_explicit(&b->flags, 0, memory_order_relaxed);
	memcpy(b + 1, p, len);
	((char *)(b + 1))[len] = 0;
	s.h.len = len;
	s.h.storage = b;
	return s;
}

static void s2_release(S2 s) {
	if (s.raw[15] & 0x80) return; /* inline: nothing to do */
	if (!s.h.storage) return;
	uint64_t f = atomic_load_explicit(&s.h.storage->flags, memory_order_relaxed);
	if (f & 3ull) return;
	uint64_t prev = atomic_fetch_sub_explicit(&s.h.storage->strong, 1, memory_order_release);
	if (prev == 1) {
		atomic_thread_fence(memory_order_acquire);
		free(s.h.storage);
	}
}

static S2 s2_retain(S2 s) {
	if (s.raw[15] & 0x80) return s;
	if (!s.h.storage) return s;
	uint64_t f = atomic_load_explicit(&s.h.storage->flags, memory_order_relaxed);
	if (f & 3ull) return s;
	atomic_fetch_add_explicit(&s.h.storage->strong, 1, memory_order_relaxed);
	return s;
}

static int64_t s2_len(S2 s) {
	return (s.raw[15] & 0x80) ? (s.raw[15] & 0x0F) : s.h.len;
}

static const char *s2_bytes(const S2 *s) {
	return (s->raw[15] & 0x80) ? (const char *)s->raw
		: (const char *)(s->h.storage + 1);
}

static int s2_eq(const S2 *a, const S2 *b) {
	int64_t la = s2_len(*a), lb = s2_len(*b);
	if (la != lb) return 0;
	if (la == 0) return 1;
	return memcmp(s2_bytes(a), s2_bytes(b), la) == 0;
}

/* ============ R3: SSO + tagged heap ============ */
typedef union S3 {
	struct { int64_t len; uintptr_t p; } h;
	unsigned char raw[16];
} S3;

static S3 s3_make(const char *p, int64_t len) {
	S3 s;
	if (len <= 15) {
		memcpy(s.raw, p, len);
		s.raw[15] = (unsigned char)(0x80 | len);
		return s;
	}
	Blk *b = malloc(sizeof(Blk) + len + 1);
	atomic_store_explicit(&b->strong, 1, memory_order_relaxed);
	memcpy(b + 1, p, len);
	((char *)(b + 1))[len] = 0;
	s.h.len = len;
	s.h.p = (uintptr_t)b;
	return s;
}

static void s3_release(S3 s) {
	if (s.raw[15] & 0x80) return;
	if ((s.h.p & 3) || s.h.p == 0) return;
	Blk *b = (Blk *)s.h.p;
	uint64_t prev = atomic_fetch_sub_explicit(&b->strong, 1, memory_order_release);
	if (prev == 1) {
		atomic_thread_fence(memory_order_acquire);
		free(b);
	}
}

/* ------------------------------ benches ------------------------------ */

#define REQS 300000

int main(void) {
	uint64_t t;
	long acc = 0;

	/* token-mix materialize+drop (9 tokens x REQS) per design */
	t = now_ns();
	for (int r = 0; r < REQS; r++) {
		int off = 0;
		for (int k = 0; k < 9; k++) {
			S0 s = s0_make(SRC + off, TOKEN_LENS[k]);
			acc += s.len;
			s0_release(s);
			off += 3;
		}
	}
	printf("RESULT r0_tokens ns_total=%llu acc=%ld\n", (unsigned long long)(now_ns() - t), acc);

	t = now_ns();
	for (int r = 0; r < REQS; r++) {
		int off = 0;
		for (int k = 0; k < 9; k++) {
			S1 s = s1_make(SRC + off, TOKEN_LENS[k]);
			acc += s.len;
			s1_release(s);
			off += 3;
		}
	}
	printf("RESULT r1_tokens ns_total=%llu acc=%ld\n", (unsigned long long)(now_ns() - t), acc);

	t = now_ns();
	for (int r = 0; r < REQS; r++) {
		int off = 0;
		for (int k = 0; k < 9; k++) {
			S2 s = s2_make(SRC + off, TOKEN_LENS[k]);
			acc += s2_len(s);
			s2_release(s);
			off += 3;
		}
	}
	printf("RESULT r2_tokens ns_total=%llu acc=%ld\n", (unsigned long long)(now_ns() - t), acc);

	t = now_ns();
	for (int r = 0; r < REQS; r++) {
		int off = 0;
		for (int k = 0; k < 9; k++) {
			S3 s = s3_make(SRC + off, TOKEN_LENS[k]);
			acc += (s.raw[15] & 0x80) ? (s.raw[15] & 0x0F) : s.h.len;
			s3_release(s);
			off += 3;
		}
	}
	printf("RESULT r3_tokens ns_total=%llu acc=%ld\n", (unsigned long long)(now_ns() - t), acc);

	/* clone+drop pair x 2M on a 7-byte heap string */
	S0 h0 = s0_make("/health", 7);
	t = now_ns();
	for (int i = 0; i < 2000000; i++) {
		S0 c = s0_retain(h0);
		acc += c.len;
		s0_release(c);
	}
	printf("RESULT r0_clone ns_total=%llu acc=%ld\n", (unsigned long long)(now_ns() - t), acc);

	S1 h1 = s1_make("/health", 7);
	t = now_ns();
	for (int i = 0; i < 2000000; i++) {
		S1 c = s1_retain(h1);
		acc += c.len;
		s1_release(c);
	}
	printf("RESULT r1_clone ns_total=%llu acc=%ld\n", (unsigned long long)(now_ns() - t), acc);

	S2 h2 = s2_make("/health", 7); /* inline: clone is free */
	(void)h2;
	t = now_ns();
	for (int i = 0; i < 2000000; i++) {
		S2 c = s2_retain(h2);
		acc += s2_len(c);
		s2_release(c);
	}
	printf("RESULT r2_clone ns_total=%llu acc=%ld\n", (unsigned long long)(now_ns() - t), acc);

	/* eq: 7-byte equal-independent, per design */
	S0 e0a = s0_make("/health", 7), e0b = s0_make("/health", 7);
	t = now_ns();
	for (int i = 0; i < 2000000; i++) acc += s0_eq(e0a, e0b);
	printf("RESULT r0_eq ns_total=%llu acc=%ld\n", (unsigned long long)(now_ns() - t), acc);

	S1 e1a = s1_make("/health", 7), e1b = s1_make("/health", 7);
	t = now_ns();
	for (int i = 0; i < 2000000; i++) acc += s1_eq(e1a, e1b);
	printf("RESULT r1_eq ns_total=%llu acc=%ld\n", (unsigned long long)(now_ns() - t), acc);

	S2 e2a = s2_make("/health", 7), e2b = s2_make("/health", 7);
	t = now_ns();
	for (int i = 0; i < 2000000; i++) acc += s2_eq(&e2a, &e2b);
	printf("RESULT r2_eq ns_total=%llu acc=%ld\n", (unsigned long long)(now_ns() - t), acc);

	printf("DONE acc=%ld\n", acc);
	return 0;
}
