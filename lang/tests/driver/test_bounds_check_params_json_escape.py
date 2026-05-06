# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression — `drift_bounds_check_params_json_build` JSON-escapes
the `container_id` field per RFC 8259 §7.

Slice 7a follow-up (K finding 3, 2026-05-05): the in-tree callers of
`drift_bounds_check_fail` all pass stdlib container-id constants
(`std.containers:Array`, `std.containers:Deque`, etc.) — none contain
`"`, `\\`, or control bytes.  The earlier "round-trip" probes
(`test_bounds_check_params_json.py`) therefore would not catch a
regression where the escape path was removed entirely.

This file links the runtime archive directly via the `cffi`-style
build harness and exercises the helper with adversarial inputs:

  * `con"tainer` — embedded `"` (ASCII 0x22) → must escape to `\\"`
  * `back\\slash` — embedded `\\` (ASCII 0x5C) → must escape to `\\\\`
  * `tab\\there` — embedded TAB (0x09) → must escape to `\\t`
  * `bell\\x07hi` — embedded BEL (0x07) → must escape to `\\u0007`

For each input, the resulting params JSON document must:
  1. Be exactly the byte sequence we expect.
  2. Parse as valid JSON (Python's `json.loads`).
  3. Round-trip the original `container_id` bytes through `json.loads`.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]


# Tiny C trampoline: links the runtime archive and exposes
# `params_build_test(container_data, container_len, idx, out_buf, out_cap)`
# as a standalone shared object loadable via ctypes.
_TRAMPOLINE_C = r"""
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "diagnostic_runtime.h"
#include "array_runtime.h"

drift_isize drift_bounds_check_params_json_build(
	struct DriftString container_id,
	drift_isize idx,
	char *out_buf,
	drift_isize out_cap);

ptrdiff_t params_build_test(
	const char *container_data,
	ptrdiff_t container_len,
	ptrdiff_t idx,
	char *out_buf,
	ptrdiff_t out_cap) {
	struct DriftString s = { container_len, (char *)container_data };
	return drift_bounds_check_params_json_build(s, idx, out_buf, out_cap);
}
"""


@pytest.fixture(scope="module")
def trampoline_lib(tmp_path_factory):
	"""Build a tiny .so that wraps `drift_bounds_check_params_json_build`
	for direct ctypes access.

	`array_runtime.c` references the live ABI 14 runtime surface
	(`drift_error_new`, `drift_error_set_params_json`,
	`drift_error_raise`, `drift_string_from_utf8_bytes`) — stub
	those so the .so loads cleanly.  `-Wl,--no-undefined` makes
	any missing symbol a link-time error rather than a dlopen-time
	surprise.

	Slice 7c-1 (ABI 14, 2026-05-06): the deleted DV-helper stubs
	(`drift_diag_from_*`, `drift_error_add_attr_dv`) are
	intentionally NOT provided here.  If `array_runtime.c`
	accidentally reintroduces a reference to any of those, the
	`-Wl,--no-undefined` link surfaces it as a missing symbol —
	without these stubs hiding the regression.  This is part of
	the same wire-cut guard as
	`test_abi_version_stamp.py::test_abi14_binary_contains_no_dv_runtime_symbols`."""
	clang = shutil.which("clang") or shutil.which("cc")
	if clang is None:
		pytest.skip("no clang/cc available")
	work = tmp_path_factory.mktemp("trampoline")
	tramp_c = work / "trampoline.c"
	tramp_c.write_text(_TRAMPOLINE_C)
	stubs_c = work / "stubs.c"
	stubs_c.write_text(r"""
#include <stddef.h>
#include <string.h>
#include "diagnostic_runtime.h"

struct DriftError;
typedef unsigned long long drift_error_code_t;

struct DriftError *drift_error_new(drift_error_code_t c, struct DriftString f) { (void)c; (void)f; return 0; }
void drift_error_set_params_json(struct DriftError *e, struct DriftString p) { (void)e; (void)p; }
__attribute__((noreturn)) void drift_error_raise(struct DriftError *e) { (void)e; for(;;); }
struct DriftString drift_string_from_utf8_bytes(const char *d, ptrdiff_t l) { struct DriftString s; s.len = l; s.data = (char *)d; return s; }
""")
	so_path = work / "libtramp.so"
	cmd = [
		clang,
		"-O0",
		"-fPIC",
		"-shared",
		"-Wl,--no-undefined",
		"-I", str(ROOT / "lang" / "language_runtime"),
		"-I", str(ROOT / "lang" / "compiler_infra"),
		str(ROOT / "lang" / "language_runtime" / "array_runtime.c"),
		str(tramp_c),
		str(stubs_c),
		"-o", str(so_path),
	]
	res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
	if res.returncode != 0:
		pytest.skip(f"trampoline link failed: {res.stderr[:1000]}")
	lib = ctypes.CDLL(str(so_path))
	lib.params_build_test.restype = ctypes.c_ssize_t
	lib.params_build_test.argtypes = [
		ctypes.c_char_p,
		ctypes.c_ssize_t,
		ctypes.c_ssize_t,
		ctypes.c_char_p,
		ctypes.c_ssize_t,
	]
	return lib


def _build(lib, container: bytes, idx: int) -> bytes:
	cap = 64 + len(container) * 6 + 64
	buf = ctypes.create_string_buffer(cap)
	n = lib.params_build_test(container, len(container), idx, buf, cap)
	assert n > 0, f"params_build_test returned {n} for container={container!r}"
	return buf.raw[:n]


def test_escapes_double_quote(trampoline_lib):
	"""Embedded `"` must JSON-escape to `\\"` so the params document
	stays well-formed."""
	out = _build(trampoline_lib, b'con"tainer', 7)
	assert out == b'{"container_id":"con\\"tainer","index":7}', (
		f"unescaped `\"` regressed: got {out!r}"
	)
	parsed = json.loads(out)
	assert parsed == {"container_id": 'con"tainer', "index": 7}


def test_escapes_backslash(trampoline_lib):
	"""Embedded `\\` must JSON-escape to `\\\\`."""
	out = _build(trampoline_lib, b'back\\slash', 9)
	assert out == b'{"container_id":"back\\\\slash","index":9}', (
		f"unescaped `\\` regressed: got {out!r}"
	)
	parsed = json.loads(out)
	assert parsed == {"container_id": "back\\slash", "index": 9}


def test_escapes_tab(trampoline_lib):
	"""Embedded TAB (0x09) must JSON-escape to `\\t`."""
	out = _build(trampoline_lib, b"col1\tcol2", 0)
	assert out == b'{"container_id":"col1\\tcol2","index":0}', (
		f"unescaped TAB regressed: got {out!r}"
	)
	parsed = json.loads(out)
	assert parsed == {"container_id": "col1\tcol2", "index": 0}


def test_escapes_bel_via_unicode(trampoline_lib):
	"""Embedded BEL (0x07) must JSON-escape to `\\u0007`."""
	out = _build(trampoline_lib, b"ring\x07ring", -1)
	assert out == b'{"container_id":"ring\\u0007ring","index":-1}', (
		f"unescaped control byte regressed: got {out!r}"
	)
	parsed = json.loads(out)
	assert parsed == {"container_id": "ring\x07ring", "index": -1}


def test_negative_index_emitted_as_signed_decimal(trampoline_lib):
	"""The index field round-trips as a JSON number (signed decimal)
	even for the in-tree all-ASCII container."""
	out = _build(trampoline_lib, b"std.containers:Array", -42)
	parsed = json.loads(out)
	assert parsed == {"container_id": "std.containers:Array", "index": -42}


def test_clean_input_unchanged(trampoline_lib):
	"""Clean ASCII input passes through unchanged — pins that the
	escape loop doesn't accidentally alter non-special bytes."""
	out = _build(trampoline_lib, b"std.containers:Array", 3)
	assert out == b'{"container_id":"std.containers:Array","index":3}'


def test_null_data_with_positive_len_returns_bad_input(trampoline_lib):
	"""Slice 7a follow-up (K finding 3 v2, 2026-05-05): a caller
	passing `data == NULL && len > 0` must NOT deref the null pointer
	in the escape loop.  The helper returns -2 (bad inputs) and the
	caller buffer is left untouched."""
	cap = 256
	buf = ctypes.create_string_buffer(cap)
	# `c_char_p(None)` -> NULL ptr; `len > 0` claims bytes that don't exist.
	rc = trampoline_lib.params_build_test(None, 5, 7, buf, cap)
	assert rc == -2, f"expected -2 (bad inputs), got {rc}"
	# Buffer should not have been written into.
	assert buf.raw[0] == 0, f"buf was written: {buf.raw[:20]!r}"


def test_zero_len_with_null_data_emits_empty_string(trampoline_lib):
	"""Edge case: zero-length container_id with NULL data is
	well-defined — the escape loop runs zero iterations and the
	output is `{"container_id":"","index":N}`."""
	cap = 256
	buf = ctypes.create_string_buffer(cap)
	rc = trampoline_lib.params_build_test(None, 0, 11, buf, cap)
	assert rc > 0, f"unexpected rc={rc}"
	assert buf.raw[:rc] == b'{"container_id":"","index":11}'


def test_short_buffer_returns_overflow(trampoline_lib):
	"""Slice 7a follow-up: the helper signals -1 on overflow rather
	than truncating or writing past `out_cap`."""
	# 3 bytes is far smaller than the 18-byte prefix.
	short = ctypes.create_string_buffer(3)
	rc = trampoline_lib.params_build_test(b"x", 1, 1, short, 3)
	assert rc == -1, f"expected -1 (overflow), got {rc}"
