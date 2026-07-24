# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""B-repr(B5) §3.3/§4.3b boundary tests for the PRIVATE
`string_bytes_base` intrinsic — the third member of the codegen layout
authority.

POSITIVE (IR-level, via a compiled program's .ll):
  * `std.ffi.with_bytes` lowers the base pointer ONCE per call —
    extractvalue field 1 + `getelementptr i8, …, 16` — and the
    surrounding std.ffi helper bodies contain NO `drift_string_retain`
    call: the intrinsic is a BORROW with no stake materialization
    (§2.8: base-once, no retain traffic inside the borrow).

NEGATIVE (checker-level):
  * calling `string_bytes_base` from OUTSIDE std.ffi is rejected with
    the std.ffi-internal diagnostic — the raw borrowed-window pointer
    is never exposed;
  * wrong arity / non-String argument are rejected.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]

POS_SRC = r"""module main;

import std.core as core;
import std.ffi as ffi;
import std.mem as mem;

pub fn main() nothrow -> Int {
	val s = "abcdef";
	val cb: core.Callback2<mem.Ptr<Byte>, Int, Int> =
		core.callback2(|p: mem.Ptr<Byte>, len: Int| => {
			val b0 = mem.ptr_read<type Byte>(p);
			cast<Int>(b0) + len
		});
	val n = ffi.with_bytes<type Int, core.Callback2<mem.Ptr<Byte>, Int, Int> >(&s, cb);
	return n - 103;  // 'a'(97) + 6
}
"""


def _compile(tmp_path: Path, src_text: str, name: str = "main"):
	src = tmp_path / f"{name}.drift"
	src.write_text(src_text)
	out_bin = tmp_path / f"{name}.bin"
	stdlib = stdlib_root() or (ROOT / "stdlib")
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev", "--allow-unsafe",
		 "--stdlib-root", str(stdlib), str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240),
	)
	return res, out_bin


def test_bytes_base_lowering_borrow_no_retain(tmp_path: Path) -> None:
	res, out_bin = _compile(tmp_path, POS_SRC)
	assert res.returncode == 0, f"{res.stdout}\n---\n{res.stderr[:2000]}"
	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=sanitizer_timeout(60))
	assert run.returncode == 0, f"exit={run.returncode}\n{run.stderr[:500]}"

	ir = Path(str(out_bin) + ".ll").read_text()
	# Locate the with_bytes instantiation body.
	m = re.search(r'define[^\n]*with_bytes[^\n]*\{(.*?)\n\}', ir, re.S)
	assert m, "with_bytes instantiation not found in IR"
	body = m.group(1)
	# base-once: exactly one +16 bytes-base GEP in the helper body.
	geps = re.findall(r"getelementptr i8, ptr %[A-Za-z0-9_.]+, i64 16\b", body)
	assert len(geps) == 1, f"expected exactly one bytes-base (+16) GEP in with_bytes; found {len(geps)}:\n{body[:1500]}"
	# borrow with NO retain: no stake materialization inside the helper.
	assert "drift_string_retain" not in body, "with_bytes must not retain (borrowed bytes base)"


NEG_OUTSIDE = r"""module main;

import std.mem as mem;

pub fn main() nothrow -> Int {
	val s = "x";
	val p = string_bytes_base(&s);
	val is_null = mem.ptr_is_null<type Byte>(p);
	if is_null { return 1; }
	return 0;
}
"""

NEG_ARITY = r"""module main;

pub fn probe() nothrow -> Int {
	val s = "x";
	return 0;
}

pub fn main() nothrow -> Int {
	return probe();
}
"""


def test_bytes_base_rejected_outside_std_ffi(tmp_path: Path) -> None:
	res, _ = _compile(tmp_path, NEG_OUTSIDE)
	assert res.returncode != 0, "string_bytes_base must not resolve outside std.ffi"
	err = res.stdout + res.stderr
	assert "std.ffi-internal" in err, f"expected the std.ffi-internal rejection:\n{err[:1200]}"


def test_bytes_base_misuse_rejected_in_std_ffi(tmp_path: Path) -> None:
	"""Arity and argument-type misuse fail even inside std.ffi — proven
	by compiling a MUTATED std.ffi that misuses the intrinsic (stdlib
	overlay copy; the real tree is untouched)."""
	import shutil

	stdlib = stdlib_root() or (ROOT / "stdlib")
	overlay = tmp_path / "stdlib"
	shutil.copytree(stdlib, overlay)
	ffi_mod = overlay / "std" / "ffi" / "ffi.drift"
	text = ffi_mod.read_text()
	marker = "fn null_byte_ptr() nothrow -> mem.Ptr<Byte> {"
	assert marker in text
	# wrong arity + non-String argument misuses, referenced from a pub fn
	text = text.replace(marker, """fn bb_misuse_arity() nothrow -> Int {
	val s = "x";
	val p = string_bytes_base(&s, 1);
	return 0;
}

fn bb_misuse_type() nothrow -> Int {
	val n = 3;
	val p = string_bytes_base(&n);
	return 0;
}

""" + marker)
	ffi_mod.write_text(text)

	src = tmp_path / "main.drift"
	src.write_text("module main;\nimport std.ffi as ffi;\npub fn main() nothrow -> Int { return 0; }\n")
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(overlay), str(src), "--entry", "main::main",
		 "-o", str(tmp_path / "bin")],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240),
	)
	assert res.returncode != 0, "intrinsic misuse inside std.ffi must be rejected"
	err = res.stdout + res.stderr
	assert "expects 1 argument" in err, f"arity misuse must be diagnosed:\n{err[:1200]}"
	assert "no matching overload for function 'string_bytes_base'" in err, (
		f"non-String argument must be diagnosed:\n{err[:1200]}"
	)
