# string-hotpath-performance-recovery: ablation-runtime generator
# (rev 2 — factorial design per review corrections 3-6).
#
# Generates modified copies of lang/language_runtime/string_runtime.c
# into bench/ablations/, forming a 2x2 factorial over
#   trace policy:  {percall (stock), cached, none}
#   validation:    {current (stock), branchlean}
# plus the ABI-21-shaped lean reference:
#
#   ab_cached_current   — launch-time-cached trace + CURRENT validation
#   ab_cached_branchlean— launch-time-cached trace + branch-lean validation
#   ab_none_current     — no trace branch + CURRENT validation   (= old ab1)
#   ab_none_branchlean  — no trace branch + branch-lean validation
#   ab_lean_ref         — no trace + ABI-21-shaped checks (attribution
#                         floor reference only; NOT a candidate)
#
# Constraints honored (corrections 4-6):
#   * cached variants read DRIFT_STR_TRACE ONCE at process init via
#     __attribute__((constructor)) into an immutable int published
#     before user threads start (documented: env must be set before
#     launch); DRIFT_STR_TRACE_FILTER stays on the already-enabled
#     slow path; enabled tracing preserved.
#   * NO eq/cmp changes in any candidate: equality validates BOTH
#     operands first (ABI-22 contract), THEN applies fast paths;
#     branch-lean touches ONLY the validate() body and keeps ALL
#     legality checks — including HAS_INTERIOR_NUL-without-NUL_SCANNED
#     — inside the combined cold-dispatch predicate.
from __future__ import annotations

import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[3]
OUT = BENCH / "ablations"

# Evidence base: the PRE-fix string_runtime.c the decision matrix was
# generated from, PRESERVED VERBATIM at
# ablations/evidence_base_string_runtime.c and hash-verified on every
# load — so regeneration and byte-for-byte comparison are ALWAYS
# enforced, independent of the tree's (post-fix) runtime source.
EVIDENCE_BASE_SHA256 = "4135459520527797460ac5a5760f40ab087cb0010de735ca1d9328de974314df"

import hashlib as _hashlib
_BASE_FILE = Path(__file__).resolve().parent / "ablations" / "evidence_base_string_runtime.c"
if not _BASE_FILE.exists():
	raise SystemExit(f"gen_ablations: preserved evidence base missing: {_BASE_FILE}")
_base_bytes = _BASE_FILE.read_bytes()
if _hashlib.sha256(_base_bytes).hexdigest() != EVIDENCE_BASE_SHA256:
	raise SystemExit(
		"gen_ablations: preserved evidence base FAILS hash verification "
		f"(expected {EVIDENCE_BASE_SHA256[:16]}...)")
SRC = _base_bytes.decode()


def replace_exactly_once(src: str, old: str, new: str) -> str:
	"""Fail closed on unexpected source shape (review residual 2):
	every intended replacement must match EXACTLY once."""
	n = src.count(old)
	if n != 1:
		raise SystemExit(
			f"gen_ablations: expected exactly 1 occurrence, found {n}:\n"
			f"--- pattern head ---\n{old[:120]}")
	return src.replace(old, new, 1)

GETENV_RETAIN = """	if (getenv("DRIFT_STR_TRACE")) {
		drift_str_trace_event("retain", s.storage,
			(const char *)drift_string_bytes_mut(s), (long)s.len, prev, prev + 1);
	}"""
GETENV_RELEASE = """	if (getenv("DRIFT_STR_TRACE")) {
		drift_str_trace_event("release", s.storage,
			(const char *)drift_string_bytes_mut(s), (long)s.len, prev, prev - 1);
	}"""

CACHED_INIT = """/* ablation: launch-time trace cache.  DRIFT_STR_TRACE is read ONCE
 * during process initialization (before user threads start) and
 * published as immutable state; setting the variable after launch has
 * no effect (documented).  DRIFT_STR_TRACE_FILTER remains a per-event
 * getenv on the already-enabled slow path only. */
static int drift_str_trace_on;
__attribute__((constructor)) static void drift_str_trace_init(void) {
	drift_str_trace_on = getenv("DRIFT_STR_TRACE") ? 1 : 0;
}

static void drift_str_trace_event(const char *what, void *hdr,"""

BRANCHLEAN_VALIDATE = """__attribute__((noinline, cold)) static void drift_string_validate_fail(DriftString s, uint64_t f) {
	if (s.storage == NULL && s.len != 0) {
		drift_contract_fail("malformed String handle: nonzero len, NULL storage");
	}
	if (s.len < 0) {
		drift_contract_fail("malformed String handle: negative len");
	}
	if (f & DRIFT_RCBYTES_RESERVED_MASK) {
		drift_contract_fail("String flags: reserved bit set");
	}
	if ((f & (DRIFT_RCBYTES_STATIC | DRIFT_RCBYTES_IMMORTAL))
			== (DRIFT_RCBYTES_STATIC | DRIFT_RCBYTES_IMMORTAL)) {
		drift_contract_fail("String flags: STATIC+IMMORTAL");
	}
	drift_contract_fail("String flags: HAS_INTERIOR_NUL without NUL_SCANNED");
}

static DriftStringCheck drift_string_validate(DriftString s) {
	/* ablation: branch-lean, IDENTICAL fail-closed coverage.  One
	 * relaxed flags load; every legality check — reserved bits,
	 * STATIC+IMMORTAL exclusion, NUL-cache coherence, negative len —
	 * folds into ONE combined predicate with a single unlikely branch
	 * into a cold outlined decoder that re-derives the exact
	 * diagnostic. */
	DriftStringCheck chk;
	if (s.storage == NULL) {
		if (__builtin_expect(s.len != 0, 0)) {
			drift_string_validate_fail(s, 0);
		}
		chk.state = DRIFT_STR_TOMBSTONE;
		chk.flags = 0;
		return chk;
	}
	uint64_t f = atomic_load_explicit(&s.storage->flags, memory_order_relaxed);
	uint64_t bad = (uint64_t)(s.len < 0)
		| (f & DRIFT_RCBYTES_RESERVED_MASK)
		| (uint64_t)((f & (DRIFT_RCBYTES_STATIC | DRIFT_RCBYTES_IMMORTAL))
			== (DRIFT_RCBYTES_STATIC | DRIFT_RCBYTES_IMMORTAL))
		| (uint64_t)((f & (DRIFT_RCBYTES_NUL_SCANNED | DRIFT_RCBYTES_HAS_INTERIOR_NUL))
			== DRIFT_RCBYTES_HAS_INTERIOR_NUL);
	if (__builtin_expect(bad != 0, 0)) {
		drift_string_validate_fail(s, f);
	}
	chk.state = DRIFT_STR_LIVE;
	chk.flags = f;
	return chk;
}
"""


def apply_trace(src: str, policy: str) -> str:
	if policy == "percall":
		return src
	if policy == "none":
		out = replace_exactly_once(src, GETENV_RETAIN, "\t/* ablation: trace branch removed */")
		out = replace_exactly_once(out, GETENV_RELEASE, "\t/* ablation: trace branch removed */")
		return out
	assert policy == "cached"
	out = replace_exactly_once(src,
		"static void drift_str_trace_event(const char *what, void *hdr,",
		CACHED_INIT)
	out = replace_exactly_once(out, GETENV_RETAIN,
		GETENV_RETAIN.replace('getenv("DRIFT_STR_TRACE")', "drift_str_trace_on"))
	out = replace_exactly_once(out, GETENV_RELEASE,
		GETENV_RELEASE.replace('getenv("DRIFT_STR_TRACE")', "drift_str_trace_on"))
	return out


def apply_validation(src: str, policy: str) -> str:
	if policy == "current":
		return src
	assert policy == "branchlean"
	start = src.index("static DriftStringCheck drift_string_validate(DriftString s) {")
	end = src.index("\n}\n", start) + 3
	return src[:start] + BRANCHLEAN_VALIDATE + src[end:]


def lean_ref(src: str) -> str:
	"""ABI-21-shaped floor reference (old ab2): no trace, minimal
	checks on retain/release/eq/cmp/concat.  NOT a candidate — kept
	solely so the attribution floor stays reproducible."""
	out = apply_trace(src, "none")

	def repl(fn_sig: str, body: str) -> None:
		nonlocal out
		start = out.index(fn_sig)
		end = out.index("\n}\n", start) + 3
		out = out[:start] + body + out[end:]

	repl("DriftString drift_string_retain(DriftString s) {", """DriftString drift_string_retain(DriftString s) {
	/* lean_ref: ABI-21-shaped */
	if (s.storage == NULL) {
		return s;
	}
	uint64_t f = atomic_load_explicit(&s.storage->flags, memory_order_relaxed);
	if (f & (DRIFT_RCBYTES_STATIC | DRIFT_RCBYTES_IMMORTAL)) {
		return s;
	}
	atomic_fetch_add_explicit(&s.storage->strong, 1, memory_order_relaxed);
	return s;
}
""")
	repl("void drift_string_release(DriftString s) {", """void drift_string_release(DriftString s) {
	/* lean_ref: ABI-21-shaped */
	if (s.storage == NULL) {
		return;
	}
	uint64_t f = atomic_load_explicit(&s.storage->flags, memory_order_relaxed);
	if (f & (DRIFT_RCBYTES_STATIC | DRIFT_RCBYTES_IMMORTAL)) {
		return;
	}
	uint64_t prev = atomic_fetch_sub_explicit(&s.storage->strong, 1, memory_order_release);
	if (prev == 0) {
		abort();
	}
	if (prev == 1) {
		atomic_thread_fence(memory_order_acquire);
		free(s.storage);
	}
}
""")
	repl("int drift_string_eq(DriftString a, DriftString b) {", """int drift_string_eq(DriftString a, DriftString b) {
	/* lean_ref: ABI-21-shaped */
	if (a.len != b.len) {
		return 0;
	}
	if (a.len == 0) {
		return 1;
	}
	if (a.storage == NULL || b.storage == NULL) {
		return 0;
	}
	return memcmp((const char *)(a.storage + 1), (const char *)(b.storage + 1), (size_t)a.len) == 0;
}
""")
	repl("int drift_string_cmp(DriftString a, DriftString b) {", """int drift_string_cmp(DriftString a, DriftString b) {
	/* lean_ref: ABI-21-shaped */
	const size_t a_len = (size_t)a.len;
	const size_t b_len = (size_t)b.len;
	const size_t min_len = a_len < b_len ? a_len : b_len;
	if (min_len > 0) {
		const int cmp = memcmp((const char *)(a.storage + 1), (const char *)(b.storage + 1), min_len);
		if (cmp != 0) {
			return cmp;
		}
	}
	if (a_len < b_len) return -1;
	if (a_len > b_len) return 1;
	return 0;
}
""")
	repl("DriftString drift_string_concat(DriftString a, DriftString b) {", """DriftString drift_string_concat(DriftString a, DriftString b) {
	/* lean_ref: no per-operand validation */
	if (b.len > DRIFT_STRING_MAX_LEN - a.len) {
		drift_contract_fail("String concat overflow");
	}
	drift_isize total = a.len + b.len;
	if (total == 0) {
		return drift_string_empty();
	}
	DriftRcBytes *blk = drift_string_alloc_block(total);
	unsigned char *out = (unsigned char *)(blk + 1);
	if (a.len > 0) {
		memcpy(out, (const char *)(a.storage + 1), (size_t)a.len);
	}
	if (b.len > 0) {
		memcpy(out + a.len, (const char *)(b.storage + 1), (size_t)b.len);
	}
	DriftString s = {total, blk};
	return s;
}
""")
	return out


def build_variants() -> dict[str, str]:
	return {
		"ab_cached_current": apply_validation(apply_trace(SRC, "cached"), "current"),
		"ab_cached_branchlean": apply_validation(apply_trace(SRC, "cached"), "branchlean"),
		"ab_none_current": apply_validation(apply_trace(SRC, "none"), "current"),
		"ab_none_branchlean": apply_validation(apply_trace(SRC, "none"), "branchlean"),
		"ab_lean_ref": lean_ref(SRC),
	}


def main():
	variants = build_variants()
	if "--check" in sys.argv:
		# regeneration must reproduce the preserved sources
		# byte-for-byte (review residual 2)
		bad = []
		for name, text in variants.items():
			f = OUT / f"{name}.c"
			if not f.exists():
				bad.append(f"{name}: MISSING")
			elif f.read_text() != text:
				bad.append(f"{name}: DIFFERS from regeneration")
		if bad:
			raise SystemExit("gen_ablations --check FAILED:\n" + "\n".join(bad))
		print(f"--check OK: {len(variants)} preserved sources reproduce byte-for-byte")
		return
	OUT.mkdir(exist_ok=True)
	for name, text in variants.items():
		(OUT / f"{name}.c").write_text(text)
	print(f"generated {len(variants)} ablation runtimes into {OUT}")


if __name__ == "__main__":
	main()
