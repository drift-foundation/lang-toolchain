#ifndef DRIFT_STRING_RUNTIME_H
#define DRIFT_STRING_RUNTIME_H

#include <stdatomic.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>

typedef ptrdiff_t drift_isize;

/* ────────────────────────────────────────────────────────────────────
 * ABI-22 String representation (B-repr/B5).
 *
 * Storage block: a 16-byte header at OFFSET 0 followed by EXACTLY
 * len + 1 tail bytes; bytes[len] == 0 always (hidden NUL — the
 * zero-copy borrowed-C-string invariant).  The ABI-21 "header behind
 * the data pointer" aliasing trick is RETIRED.
 *
 * LAYOUT AUTHORITY: only string_runtime.{h,c} on the C side and the
 * compiler codegen layout authority (literal emitters, StringByteAt,
 * string_bytes_base intrinsic) may inspect storage/header layout.
 * Every other consumer — runtime C included — goes through the
 * accessors below (enforced by test_string_layout_audit.py).
 * ──────────────────────────────────────────────────────────────────── */

typedef struct DriftRcBytes {
	_Atomic uint64_t strong; /* offset 0 */
	_Atomic uint64_t flags;  /* offset 8 — atomic: interior-NUL knowledge
	                          * is cached lazily on immutable blocks
	                          * shared across threads */
	/* unsigned char bytes[];   offset 16 (tail), EXACTLY len+1 bytes;
	 *                          bytes[len] == 0 */
} DriftRcBytes;

_Static_assert(sizeof(DriftRcBytes) == 16, "DriftRcBytes header must be exactly 16 bytes");
_Static_assert(_Alignof(DriftRcBytes) == 8, "DriftRcBytes must be 8-byte aligned");
_Static_assert(offsetof(DriftRcBytes, strong) == 0, "DriftRcBytes strong must sit at offset 0");
_Static_assert(offsetof(DriftRcBytes, flags) == 8, "DriftRcBytes flags must sit at offset 8");

typedef struct DriftString {
	drift_isize len;        /* inline byte length (excludes hidden NUL) */
	DriftRcBytes *storage;  /* -> header at OFFSET 0; bytes at +16 */
} DriftString;

/* RcBytesFlags (u64).  All other bits reserved-zero; a set reserved bit
 * is an UNCONDITIONAL contract failure.
 *
 * STATIC and IMMORTAL are MUTUALLY EXCLUSIVE: compiler rodata literals
 * carry STATIC only; runtime-owned never-freed blocks (the empty
 * singleton, from_bool constants) carry IMMORTAL only.  Both set =
 * contract failure.
 *
 * NUL-cache state machine (monotonic, write-once):
 *   (NUL_SCANNED, HAS_INTERIOR_NUL) = (0,0) unknown — readers scan and
 *   may cache; (1,0) scanned, no interior NUL (the zero-copy borrowed
 *   C-string fast path); (1,1) scanned, has interior NUL; (0,1) ILLEGAL.
 *   The only legal transition is one relaxed atomic_fetch_or from the
 *   unknown state; concurrent scanners race benignly to identical bits.
 */
enum {
	DRIFT_RCBYTES_STATIC           = 1ULL << 0,
	DRIFT_RCBYTES_IMMORTAL         = 1ULL << 1,
	DRIFT_RCBYTES_NUL_SCANNED      = 1ULL << 2,
	DRIFT_RCBYTES_HAS_INTERIOR_NUL = 1ULL << 3,
};
#define DRIFT_RCBYTES_RESERVED_MASK (~(uint64_t)0xF)

/* Refcount overflow guard threshold: fail closed at prev >= MAX_LIVE
 * (~2^63 live stakes — unreachable by real programs, checked
 * unconditionally in normal AND NDEBUG builds). */
#define DRIFT_RC_MAX_LIVE (UINT64_MAX / 2)

/* Maximum byte length a String allocation may carry:
 * header + len + 1 hidden NUL must fit in ptrdiff_t. */
#define DRIFT_STRING_MAX_LEN ((drift_isize)(PTRDIFF_MAX - (drift_isize)sizeof(DriftRcBytes) - 1))

/* Unconditional runtime contract failure: prints
 * "[drift:contract] <what>\n" to stderr and abort()s.  IDENTICAL in
 * normal and NDEBUG/release runtime variants — tombstone/malformed-
 * handle failures are never debug-only. */
_Noreturn void drift_contract_fail(const char *what);

/* The canonical empty-String singleton (ABI-22 symbol).  Every
 * source-level empty String ({0-length literal, empty constructor
 * results, empty concat}) resolves to this ONE runtime-owned immortal
 * block: len 0, storage -> {IMMORTAL|NUL_SCANNED} header, one NUL tail
 * byte.  External linkage with HIDDEN visibility: referenceable by
 * codegen and the runtime, not a public API symbol.  Pointer identity
 * is NOT String semantics. */
struct DriftEmptyString {
	DriftRcBytes hdr;
	unsigned char nul[1];
};
extern const struct DriftEmptyString __drift_rt_string_empty
	__attribute__((visibility("hidden")));

DriftString drift_string_empty(void);

/* ── Read-only accessors over LIVE handles (decision 6) ──────────────
 * Canonical empty: len() == 0, data() == non-null pointer to the
 * singleton's trailing NUL; C-string conversion succeeds.
 * Tombstone {0, NULL}: CONTRACT FAILURE — the all-zero handle is a
 * drop-only sentinel (compiler zero-storage doctrine), never a
 * readable value; silently reading it as "" would mask a
 * use-after-move.
 * Malformed ({len != 0, NULL} or len < 0): contract failure.
 * On live handles: data(s) == (const unsigned char *)(s.storage + 1)
 * and data(s)[len] == 0 (hidden NUL). */
/* Shared observation prologue: tombstone / malformed-handle / illegal-
 * flag states all fail closed (UNCONDITIONAL — identical in normal and
 * NDEBUG builds), per the pinned §2.3/§2.6 contract. */
static inline void drift_string_observe_validate(DriftString s, const char *tomb_what) {
	if (s.storage == NULL) {
		if (s.len != 0) {
			drift_contract_fail("malformed String handle: nonzero len, NULL storage");
		}
		drift_contract_fail(tomb_what);
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
}

static inline drift_isize drift_string_len(DriftString s) {
	drift_string_observe_validate(s, "String tombstone observed (len)");
	return s.len;
}

static inline const unsigned char *drift_string_data(DriftString s) {
	drift_string_observe_validate(s, "String tombstone observed (data)");
	return (const unsigned char *)(s.storage + 1);
}

/* ── Constructors / helpers (by-value two-word handle, as ABI-21) ── */
DriftString drift_string_literal(const char *data, drift_isize len);
DriftString drift_string_from_cstr(const char *cstr);
DriftString drift_string_from_utf8_bytes(const char *data, drift_isize len);
DriftString drift_string_from_int64(int64_t v);
DriftString drift_string_from_uint64(uint64_t v);
// Deterministic float formatting (Ryu) for Drift `Float` once the type exists end-to-end.
DriftString drift_string_from_f64(double v);
DriftString drift_string_from_bool(int v);
DriftString drift_string_concat(DriftString a, DriftString b);
int drift_string_eq(DriftString a, DriftString b);
DriftString drift_string_retain(DriftString s);
void drift_string_release(DriftString s);
// DriftString lexicographic comparison by unsigned bytes.
//
// This is a deterministic, locale-independent ordering suitable for the
// language-level `String` comparison operators (`<`, `<=`, `>`, `>=`).
//
// Contract:
//   - Returns <0 if a < b, 0 if a == b, >0 if a > b.
//   - Comparison is lexicographic on the underlying UTF-8 byte sequences
//     (i.e., unsigned byte comparison), with shorter prefix ordering first.
int drift_string_cmp(DriftString a, DriftString b);
void drift_string_free(DriftString s);
char *drift_string_to_cstr(DriftString s);

/* ── C-string bridge helpers (§3.3) — pointer-taking, borrowed input ──
 * Declared to Drift as `&String` (const DriftString *): borrowed, no
 * by-value stake protocol. */

/* First interior-NUL byte index in *s, or -1 if none.  Owns the
 * NUL-cache protocol: relaxed flags load; one relaxed fetch_or publish;
 * EXISTENCE is cached in the flags, the exact INDEX is re-scanned on
 * the error path.  Tombstone/malformed input -> contract failure. */
drift_isize drift_string_interior_nul_index(const DriftString *s);

/* Owned NUL-terminated copy of *s: interior NUL -> returns NULL and
 * writes the first interior-NUL index to *nul_index_out; else returns
 * a malloc'd cstring (free with drift_cstr_free) and writes -1.
 * Allocator identity stays inside the runtime (malloc/free); C code
 * taking ownership must be malloc/free-compatible. */
char *drift_string_to_owned_cstr(const DriftString *s, drift_isize *nul_index_out);
/* UNCHECKED owned NUL-terminated copy: interior NULs are preserved in
 * the block (C sees a truncated string at the first interior NUL — the
 * caller has accepted that hazard).  Pairs with drift_cstr_free. */
char *drift_string_to_owned_cstr_unchecked(const DriftString *s);
void drift_cstr_free(char *p);

typedef struct DriftCBytes {
	unsigned char *ptr;
	drift_isize len;
} DriftCBytes;

/* Infallible owned byte copy of *s (interior NULs preserved; a hidden
 * trailing NUL is still appended after len bytes).  Free with
 * drift_cbytes_free. */
DriftCBytes drift_string_to_owned_cbytes(const DriftString *s);
void drift_cbytes_free(DriftCBytes b);

/* By-value DriftString ABI -- Convention A (normal extern receivers):
 * The Drift caller emits `retain(s); extern(s); release(s)` around
 * the call, transferring an extra refcount stake to the C callee.
 * The callee MUST release that stake exactly once before returning.
 * Annotate received-by-value parameters with DRIFT_OWNED_STRING
 * (using a local copy) to make the release automatic at every scope
 * exit -- no per-return-path drift_string_release() calls needed.
 *
 * Convention B (borrowed-pass-through receivers) -- do NOT use
 * DRIFT_OWNED_STRING.  The Drift caller passes the existing stake
 * direct (no pre-retain) and releases its own local AFTER the call.
 * Adding the macro on these sites would double-free (UAF on heap
 * inputs).  Convention-B receivers must instead carry an explicit
 * drift-owned-string-audit allow marker (read-only-borrow /
 * consumed-by-noreturn-callee as appropriate).
 *
 * IMPORTANT: @intrinsic-ness does NOT decide the convention -- the
 * DRIFT-LEVEL CALL SITE does.  An intrinsic whose stdlib callers pass
 * `move s` (or a stake copy) transfers ownership and is Convention A
 * even with no pre-retain: the move IS the transfer, the caller can
 * no longer release, and the callee must (console_write/_writeln,
 * exec_set_name, vt_set_op -- the latter two proven by a heap-string
 * valgrind probe both directions, 2026-07-12).  Convention B applies
 * where callers keep ownership and pass a live borrowed value
 * (diagnostic paths: assert/bounds-check sites).
 *
 * There is deliberately NO exhaustive site list here -- it went stale
 * once already.  The authority is the audit
 * (lang/tests/driver/test_drift_owned_string_audit.py); enumerate
 * current Convention-B sites with:
 *   grep -rn "drift-owned-string-audit: allow" lang/language_runtime/
 *
 * Adoption (both conventions) is enforced by
 * lang/tests/driver/test_drift_owned_string_audit.py, which fails CI
 * if a by-value DriftString receiver lacks either the macro or an
 * explicit drift-owned-string-audit allow marker. */
static inline void _drift_string_cleanup(DriftString *s) {
	drift_string_release(*s);
}
#define DRIFT_OWNED_STRING __attribute__((cleanup(_drift_string_cleanup)))

#endif  // DRIFT_STRING_RUNTIME_H
