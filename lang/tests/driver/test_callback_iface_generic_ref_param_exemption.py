# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""LANGUAGE_BUG #5 regression: boxed-callback interface dispatch wrongly
rejected the ratified-LEGAL generic-by-value spelling `cb.call(&mut scope)`.

Builtin `Callback*` interface schemas reference their type params via
`param_index` with an EMPTY-STRING `name` (""), while the shared
classifier's param_index exemption required `name is None` — param_index
must be authoritative unconditionally.  So
`Callback1<&mut Scope, R>.call(&mut s)` — policy-matrix row 10, the
release-notes' own "still doing real work" example — read as a declared
`&mut` formal and fired E_REDUNDANT_ARG_BORROW.  First seen live at
`stdlib/std/ffi/ffi.drift:428` (`with_cstring_scope`'s `body.call(&mut
s)`) when the memcheck lane's driver shard ran `test_b5_ffi_api_teeth`;
the pre-existing Fn1 pin never caught it because it specializes F to a
THIN function, which D8 exempts before this path is reached.

Four full compile/run rows: {direct Callback1 value, `require F is Fn1`
wrapper instantiated with a boxed callback} × {&mut Scope, &Scope}.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]


def _build_and_run(tmp_path: Path, src_text: str) -> None:
	src = tmp_path / "main.drift"
	src.write_text(src_text)
	out = tmp_path / "x.bin"
	comp = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240),
	)
	assert comp.returncode == 0, (comp.stderr + comp.stdout)[-1500:]
	err = comp.stderr + comp.stdout
	assert "E_REDUNDANT_ARG_BORROW" not in err, err[-1500:]
	run = subprocess.run(
		[str(out)], capture_output=True, text=True,
		timeout=sanitizer_timeout(120),
	)
	assert run.returncode == 0, f"rc={run.returncode}\n{run.stderr[-800:]}"


_MUT_PRELUDE = """\
module main;
import std.core as core;

pub struct Scope2 { pub n: Int }
"""


def test_direct_boxed_callback_mut_ref_generic_param(tmp_path: Path) -> None:
	"""`Callback1<&mut Scope2, Int>.call(&mut s)` — the explicit borrow at
	a generic-by-value formal instantiated at `&mut` is LEGAL (row 10)."""
	_build_and_run(tmp_path, _MUT_PRELUDE + """
pub fn main() nothrow -> Int {
	val cb: core.Callback1<&mut Scope2, Int> =
		core.callback1(|sc: &mut Scope2| => { sc.n = sc.n + 41; return sc.n; });
	var s = Scope2(n = 1);
	val r = cb.call(&mut s);
	if r != 42 { return 1; }
	if s.n != 42 { return 2; }
	return 0;
}
""")


def test_direct_boxed_callback_shared_ref_generic_param(tmp_path: Path) -> None:
	"""Shared mirror: `Callback1<&Scope2, Int>.call(&s)` is LEGAL."""
	_build_and_run(tmp_path, _MUT_PRELUDE + """
pub fn main() nothrow -> Int {
	val cb: core.Callback1<&Scope2, Int> =
		core.callback1(|sc: &Scope2| => { return sc.n + 41; });
	val s = Scope2(n = 1);
	val r = cb.call(&s);
	if r != 42 { return 1; }
	return 0;
}
""")


def test_fn1_require_wrapper_with_boxed_callback_mut(tmp_path: Path) -> None:
	"""The exact `with_cstring_scope` shape: a generic wrapper whose
	`require F is core.Fn1<&mut Scope2, T>` body does `body.call(&mut s)`,
	instantiated with a BOXED Callback1 (interface dispatch, not the
	D8-exempt thin-fn specialization the pre-existing pin used)."""
	_build_and_run(tmp_path, _MUT_PRELUDE + """
fn run_with<T, F>(body: F) nothrow -> T require F is core.Fn1<&mut Scope2, T> {
	var s = Scope2(n = 1);
	return body.call(&mut s);
}

pub fn main() nothrow -> Int {
	val cb: core.Callback1<&mut Scope2, Int> =
		core.callback1(|sc: &mut Scope2| => { sc.n = sc.n + 41; return sc.n; });
	val r: Int = run_with<type Int, core.Callback1<&mut Scope2, Int> >(move cb);
	if r != 42 { return 1; }
	return 0;
}
""")


def test_fn1_require_wrapper_with_boxed_callback_shared(tmp_path: Path) -> None:
	"""Shared mirror of the wrapper shape."""
	_build_and_run(tmp_path, _MUT_PRELUDE + """
fn run_with<T, F>(body: F) nothrow -> T require F is core.Fn1<&Scope2, T> {
	val s = Scope2(n = 1);
	return body.call(&s);
}

pub fn main() nothrow -> Int {
	val cb: core.Callback1<&Scope2, Int> =
		core.callback1(|sc: &Scope2| => { return sc.n + 41; });
	val r: Int = run_with<type Int, core.Callback1<&Scope2, Int> >(move cb);
	if r != 42 { return 1; }
	return 0;
}
""")
