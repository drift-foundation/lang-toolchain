# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Regression: direct field access on a non-Copy `Arc<Interface>` field
through a borrowed receiver (`h.arc.clone()`, `h.arc.get().v()`) must
produce correct results — no UAF, no double-drop.

This test validates the natural source shape that std.log uses for
its resolver:
  - `cfg.resolver.clone()`
  - `st.resolver.get().resolve()`

The natural shape works correctly as long as the enclosing struct is
NOT marked `core.Copy` (bit-copying a struct with a non-Copy Arc field
creates an unretained duplicate whose destructor double-frees).  The
companion test `test_copy_impl_noncopy_field_rejected.py` ensures the
compiler rejects such Copy impls.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main
from lang.driftc.parser import stdlib_root

from lang.codegen.llvm.test_utils import sanitizer_timeout


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _compile_and_run(
	tmp_path: Path,
	source: str,
	capsys: pytest.CaptureFixture[str],
) -> tuple[int, int]:
	"""Compile `source` to an executable; run it; return (compile_rc, run_rc)."""
	mod_root = tmp_path / "mods"
	main_src = mod_root / "main" / "main.drift"
	_write_file(main_src, source)
	exe = tmp_path / "out"
	root = stdlib_root()
	args = [
		"-M", str(mod_root),
		str(main_src),
		"-o", str(exe),
		"--dev",
		"--json",
	]
	if root:
		args += ["--stdlib-root", str(root)]
	rc = driftc_main(args)
	capsys.readouterr()
	if rc != 0:
		return rc, -1
	result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=sanitizer_timeout(10))
	return 0, result.returncode


_DIRECT_FIELD_ACCESS = """
module main;

import std.concurrent as conc;

pub interface I {
	fn v(self: &Self) nothrow -> Int;
}

pub struct S {
	pub n: Int
}

implement I for S {
	pub fn v(self: &S) nothrow -> Int {
		return self.n;
	}
}

struct Holder {
	arc: conc.Arc<I>
}

// Natural form: direct field access + method call.
// When the compiler correctly handles non-Copy field projection
// through a borrowed receiver, this must NOT byte-copy the Arc
// without retain.
fn clone_direct(h: &Holder) nothrow -> conc.Arc<I> {
	return h.arc.clone();
}

fn dispatch_direct(h: &Holder) nothrow -> Int {
	return h.arc.get().v();
}

fn main() nothrow -> Int {
	val original = conc.arc(S(n = 7)).as_interface<type I>();
	val h = Holder(arc = move original);
	val cloned = clone_direct(&h);
	val r1 = cloned.get().v();
	val r2 = dispatch_direct(&h);
	if r1 != 7 { return 1; }
	if r2 != 7 { return 2; }
	return 0;
}
""".lstrip()


def test_direct_noncopy_field_access_through_borrow(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Direct `h.arc.clone()` and `h.arc.get().v()` through a borrowed
	receiver must work correctly without UAF.  The compiler must
	project to the field via GEP and not byte-copy the entire struct
	(which would include the non-Copy Arc field without retain)."""
	compile_rc, run_rc = _compile_and_run(tmp_path, _DIRECT_FIELD_ACCESS, capsys)
	assert compile_rc == 0, f"compile failed: rc={compile_rc}"
	assert run_rc == 0, (
		f"direct field access through borrow returned {run_rc}, expected 0 "
		f"(non-zero indicates UAF or incorrect dispatch)"
	)
