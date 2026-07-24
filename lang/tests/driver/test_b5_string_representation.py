# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""B-repr(B5) ABI-22 String representation battery (§7 acceptance rows:
representation pins / malformed-state / tombstone / empty-singleton /
NUL-cache / overflow / contract-failure-in-both-builds).

Compiles `string_runtime.c` DIRECTLY (plus a C test main) in BOTH the
normal and NDEBUG/optimized configurations and proves, per §2:

  * layout pins compile (the `_Static_assert` battery lives in the
    header and gates every build here);
  * hidden trailing NUL after every constructor; empty-singleton
    identity for "" / empty concat; bool constants immortal;
  * NUL-cache monotonicity (one relaxed fetch_or; second query served
    from the cache; STATIC/IMMORTAL populations pre-scanned);
  * the reserved zero tombstone is DROP-ONLY: release/free no-op, and
    EVERY value observation (len/data/eq/cmp/concat/to_cstr/
    interior_nul_index/to_owned_*) plus retain FAILS CLOSED with
    `[drift:contract]` — in BOTH runtime builds (never NDEBUG-gated);
  * malformed handles ({len != 0, NULL}, negative len) fail closed in
    every entry point INCLUDING release; illegal flag states
    (STATIC+IMMORTAL, HAS_INTERIOR_NUL without NUL_SCANNED, reserved
    bits) fail closed;
  * constructor edges: NULL cstr / NULL bytes with len > 0 / negative
    len / length overflow abort (never silently empty);
  * refcount overflow guard fails closed at DRIFT_RC_MAX_LIVE.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]
RUNTIME = ROOT / "lang" / "language_runtime"

BUILD_MODES = {
	"normal": [],
	"ndebug": ["-DNDEBUG", "-O2"],
}

POSITIVE_MAIN = r"""
#include "string_runtime.h"
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>

static int fail(const char *what) { fprintf(stderr, "FAIL: %s\n", what); return 1; }

int main(void) {
	/* empty singleton identity: "" constructors + empty concat all
	 * resolve to THE one immortal block */
	DriftString e1 = drift_string_from_utf8_bytes("x", 0);
	DriftString e2 = drift_string_empty();
	DriftString e3 = drift_string_concat(e1, e2);
	if (e1.storage != (DriftRcBytes *)&__drift_rt_string_empty.hdr) return fail("e1 singleton");
	if (e2.storage != e1.storage || e3.storage != e1.storage) return fail("singleton identity");
	if (drift_string_len(e1) != 0) return fail("empty len");
	if (drift_string_data(e1) == NULL || drift_string_data(e1)[0] != 0) return fail("empty data NUL");
	char *ec = drift_string_to_cstr(e1);
	if (!ec || ec[0] != 0) return fail("empty to_cstr");
	free(ec);
	drift_string_release(e1); /* immortal no-op */

	/* hidden NUL after every constructor */
	DriftString a = drift_string_from_cstr("abc");
	DriftString b = drift_string_from_utf8_bytes("de", 2);
	DriftString i = drift_string_from_int64(-42);
	DriftString u = drift_string_from_uint64(7);
	DriftString f = drift_string_from_f64(1.5);
	DriftString t = drift_string_from_bool(1);
	DriftString l = drift_string_literal("lit", 3);
	DriftString c = drift_string_concat(a, b);
	DriftString all[] = {a, b, i, u, f, t, l, c};
	for (unsigned k = 0; k < sizeof(all)/sizeof(all[0]); k++) {
		const unsigned char *d = drift_string_data(all[k]);
		if (d[drift_string_len(all[k])] != 0) return fail("hidden NUL");
	}
	if (drift_string_len(c) != 5 || memcmp(drift_string_data(c), "abcde", 5) != 0) return fail("concat bytes");

	/* bool constants: immortal, pre-scanned */
	uint64_t tf = atomic_load_explicit(&t.storage->flags, memory_order_relaxed);
	if (!(tf & DRIFT_RCBYTES_IMMORTAL) || (tf & DRIFT_RCBYTES_STATIC)) return fail("bool immortal");
	if (!(tf & DRIFT_RCBYTES_NUL_SCANNED)) return fail("bool prescanned");

	/* NUL-cache monotonicity: unknown -> one fetch_or -> stable */
	DriftString n = drift_string_from_utf8_bytes("q\0r", 3);
	uint64_t f0 = atomic_load_explicit(&n.storage->flags, memory_order_relaxed);
	if (f0 & (DRIFT_RCBYTES_NUL_SCANNED | DRIFT_RCBYTES_HAS_INTERIOR_NUL)) return fail("fresh alloc unscanned");
	if (drift_string_interior_nul_index(&n) != 1) return fail("nul index");
	uint64_t f1 = atomic_load_explicit(&n.storage->flags, memory_order_relaxed);
	if (!(f1 & DRIFT_RCBYTES_NUL_SCANNED) || !(f1 & DRIFT_RCBYTES_HAS_INTERIOR_NUL)) return fail("cache published");
	if (drift_string_interior_nul_index(&n) != 1) return fail("cached nul index");
	uint64_t f2 = atomic_load_explicit(&n.storage->flags, memory_order_relaxed);
	if (f2 != f1) return fail("cache monotonic");
	DriftString cl = drift_string_from_cstr("clean");
	if (drift_string_interior_nul_index(&cl) != -1) return fail("clean scan");
	uint64_t f3 = atomic_load_explicit(&cl.storage->flags, memory_order_relaxed);
	if (!(f3 & DRIFT_RCBYTES_NUL_SCANNED) || (f3 & DRIFT_RCBYTES_HAS_INTERIOR_NUL)) return fail("clean cache");

	/* owned copies */
	drift_isize idx = 99;
	char *oc = drift_string_to_owned_cstr(&cl, &idx);
	if (!oc || idx != -1 || strcmp(oc, "clean") != 0) return fail("owned cstr");
	drift_cstr_free(oc);
	if (drift_string_to_owned_cstr(&n, &idx) != NULL || idx != 1) return fail("owned cstr interior");
	DriftCBytes cb = drift_string_to_owned_cbytes(&n);
	if (cb.len != 3 || cb.ptr[0] != 'q' || cb.ptr[1] != 0 || cb.ptr[2] != 'r' || cb.ptr[3] != 0) return fail("owned cbytes");
	drift_cbytes_free(cb);

	/* retain/release round trip; tombstone drop-only no-op */
	DriftString a2 = drift_string_retain(a);
	drift_string_release(a2);
	DriftString z = {0, NULL};
	drift_string_release(z);
	drift_string_free(z);

	drift_string_release(a); drift_string_release(b); drift_string_release(i);
	drift_string_release(u); drift_string_release(f); drift_string_release(t);
	drift_string_release(l); drift_string_release(c); drift_string_release(n);
	drift_string_release(cl);
	printf("REPR-OK\n");
	return 0;
}
"""

# Each entry: (name, C statements that must abort with [drift:contract]
# or plain abort).  `PRE` provides shared locals.
PRE = r"""
	DriftString z = {0, NULL};
	DriftString live = drift_string_from_cstr("x");
	(void)live;
"""

ABORT_CASES = {
	# tombstone observation fails closed (drop-only sentinel)
	"tomb_len": ("(void)drift_string_len(z);", "[drift:contract]"),
	"tomb_data": ("(void)drift_string_data(z);", "[drift:contract]"),
	"tomb_to_cstr": ("(void)drift_string_to_cstr(z);", "[drift:contract]"),
	"tomb_eq": ("(void)drift_string_eq(z, live);", "[drift:contract]"),
	"tomb_cmp": ("(void)drift_string_cmp(live, z);", "[drift:contract]"),
	"tomb_concat": ("(void)drift_string_concat(z, live);", "[drift:contract]"),
	"tomb_retain": ("(void)drift_string_retain(z);", "retain of String tombstone"),
	"tomb_nul_index": ("(void)drift_string_interior_nul_index(&z);", "[drift:contract]"),
	"tomb_owned_cstr": ("drift_isize ix; (void)drift_string_to_owned_cstr(&z, &ix);", "[drift:contract]"),
	"tomb_owned_cbytes": ("(void)drift_string_to_owned_cbytes(&z);", "[drift:contract]"),
	# malformed handles fail closed EVERYWHERE, including release
	"malformed_len_nonzero_null": ("DriftString m = {3, NULL}; (void)drift_string_len(m);", "nonzero len, NULL storage"),
	"malformed_release": ("DriftString m = {3, NULL}; drift_string_release(m);", "nonzero len, NULL storage"),
	"malformed_negative": ("DriftString m = {-1, live.storage}; (void)drift_string_data(m);", "negative len"),
	# illegal flag states (unconditional, never NDEBUG-gated) — via the
	# refcount path AND the observation accessors (finding: accessors
	# must validate flags too)
	"flags_reserved_len": (
		"atomic_store_explicit(&live.storage->flags, 1ULL << 21, memory_order_relaxed);"
		" (void)drift_string_len(live);", "reserved bit"),
	"flags_static_immortal_data": (
		"atomic_store_explicit(&live.storage->flags, DRIFT_RCBYTES_STATIC | DRIFT_RCBYTES_IMMORTAL, memory_order_relaxed);"
		" (void)drift_string_data(live);", "STATIC+IMMORTAL"),
	"flags_orphan_len": (
		"atomic_store_explicit(&live.storage->flags, DRIFT_RCBYTES_HAS_INTERIOR_NUL, memory_order_relaxed);"
		" (void)drift_string_len(live);", "HAS_INTERIOR_NUL without NUL_SCANNED"),
	"flags_static_immortal": (
		"atomic_store_explicit(&live.storage->flags, DRIFT_RCBYTES_STATIC | DRIFT_RCBYTES_IMMORTAL, memory_order_relaxed);"
		" (void)drift_string_retain(live);", "STATIC+IMMORTAL"),
	"flags_orphan_interior": (
		"atomic_store_explicit(&live.storage->flags, DRIFT_RCBYTES_HAS_INTERIOR_NUL, memory_order_relaxed);"
		" (void)drift_string_retain(live);", "HAS_INTERIOR_NUL without NUL_SCANNED"),
	"flags_reserved_bit": (
		"atomic_store_explicit(&live.storage->flags, 1ULL << 17, memory_order_relaxed);"
		" (void)drift_string_retain(live);", "reserved bit"),
	# constructor edges: invalid input NEVER silently empty
	"ctor_null_cstr": ("(void)drift_string_from_cstr(NULL);", "NULL cstr"),
	"ctor_null_bytes": ("(void)drift_string_from_utf8_bytes(NULL, 2);", "NULL bytes"),
	"ctor_negative_len": ("(void)drift_string_from_utf8_bytes(\"x\", -2);", "negative String length"),
	"ctor_len_overflow": ("(void)drift_string_from_utf8_bytes(\"x\", DRIFT_STRING_MAX_LEN + 1);", "String length overflow"),
	"concat_overflow": (
		"DriftString big = {DRIFT_STRING_MAX_LEN, live.storage};"
		" (void)drift_string_concat(big, live);", "String concat overflow"),
	# refcount overflow guard (>= DRIFT_RC_MAX_LIVE)
	"refcount_overflow": (
		"atomic_store_explicit(&live.storage->strong, DRIFT_RC_MAX_LIVE, memory_order_relaxed);"
		" (void)drift_string_retain(live);", "String refcount overflow"),
	# release underflow aborts (B0 behavior kept; plain abort, no message pin)
	"release_underflow": (
		"atomic_store_explicit(&live.storage->strong, 0, memory_order_relaxed);"
		" drift_string_release(live);", None),
}


def _compile_c(tmp_path: Path, name: str, main_src: str, mode: str) -> Path:
	src = tmp_path / f"{name}_{mode}.c"
	src.write_text(main_src)
	out = tmp_path / f"{name}_{mode}.bin"
	cmd = ["/usr/bin/clang", "-std=gnu11", "-Wall", *BUILD_MODES[mode],
		"-I", str(RUNTIME),
		str(RUNTIME / "string_runtime.c"), str(RUNTIME / "ryu_d2s.c"), str(src),
		"-o", str(out)]
	res = subprocess.run(cmd, capture_output=True, text=True, timeout=sanitizer_timeout(120))
	assert res.returncode == 0, f"C compile failed ({mode}):\n{res.stderr[:2000]}"
	return out


@pytest.mark.parametrize("mode", sorted(BUILD_MODES))
def test_representation_positive_battery(tmp_path: Path, mode: str) -> None:
	out = _compile_c(tmp_path, "repr_pos", POSITIVE_MAIN, mode)
	res = subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(60))
	assert res.returncode == 0 and "REPR-OK" in res.stdout, (
		f"[{mode}] {res.returncode}\n{res.stdout}\n{res.stderr[:1000]}"
	)


@pytest.mark.parametrize("mode", sorted(BUILD_MODES))
def test_contract_failures_abort_in_both_builds(tmp_path: Path, mode: str) -> None:
	"""Every §2.6 observation/retain trap, malformed-handle case, illegal
	flag state, constructor edge, and the refcount guard ABORTS — in the
	normal AND the NDEBUG/optimized runtime (never debug-only)."""
	for name, (stmt, needle) in sorted(ABORT_CASES.items()):
		main_src = (
			'#include "string_runtime.h"\n#include <stdatomic.h>\n#include <stdio.h>\n'
			"int main(void) {\n" + PRE + "\t" + stmt + "\n\tprintf(\"NO-ABORT\\n\");\n\treturn 0;\n}\n"
		)
		out = _compile_c(tmp_path, f"abort_{name}", main_src, mode)
		res = subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(60))
		assert res.returncode != 0, f"[{mode}] {name}: expected abort, got exit 0 ({res.stdout!r})"
		assert "NO-ABORT" not in res.stdout, f"[{mode}] {name}: guard did not fire before completion"
		if needle:
			assert needle in res.stderr, f"[{mode}] {name}: missing contract message {needle!r}:\n{res.stderr[:500]}"
