# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Stage2 (HIR → MIR) lowering pins for the standalone
`MaybeUninit<T>` local pattern landed at 0.31.16.

Surface under test
------------------
`mem.maybe_uninit<type T>()` is the only constructor for a
`MaybeUninit<T>` value visible to user code.  Pre-0.31.16 its
HIR→MIR lowering was a hard `NotImplementedError`, blocking the
standalone-local pattern:

\tvar slot = mem.maybe_uninit<type T>();
\tmem.maybe_write<type T>(&mut slot, move v);
\tval out = mem.maybe_assume_init_read<type T>(&mut slot);

The audited single-op MIR shape for the constructor is
`ZeroValue(dest=temp, ty=MaybeUninit<T>)`.  The receiving local is
then `StoreLocal`-ed by the standard `var = expr` machinery.  The
type's empty-struct definition combined with the four-site codegen
unwrap (`llvm_codegen.py::llvm_type_for_typeid`,
`_llvm_type_for_typeid`, `_llvm_storage_type_for_typeid`,
`_size_align_typeid`) makes `sizeof(MaybeUninit<T>) == sizeof(T)`,
so the zero-init covers the full `T`-shaped slot.

Pins in this file are driver-style: compile a real fixture under
`--emit-ir`, inspect the resulting LLVM IR for the expected
lowering signature.  This mirrors `test_consume_intrinsic_ownership.py`
which is the established stage2 driver-pin convention for
intrinsic lowerings.

Why driver-style and not synthetic-HIR
--------------------------------------
The intrinsic call path goes through `call_resolver` (type-arg
inference, signature construction) and `call_contract` (arity
validation) before reaching HIR→MIR.  Building an HIR `HCall`
plus matching `CallInfo` with `IntrinsicKind.MAYBE_UNINIT` by hand
is more brittle than compiling the user-facing surface and
asserting the post-codegen shape.

Coverage map
------------
The container path (`RawBuffer<MaybeUninit<T>>`) is covered by
`lang/tests/codegen/e2e/maybe_assume_init_read_moves_out_no_leak/`
through HashMapCore.  These pins cover the standalone-local path
that was previously unreachable.  The double-drop / consume-by-
intrinsic invariant is pinned by the sibling
`test_consume_intrinsic_ownership.py::
test_lowering_mem_maybe_write_emits_moveout_for_non_copy_local`.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[3]


def _compile_to_ir(tmp_path: Path, source: str) -> str:
	src = tmp_path / "main.drift"
	src.write_text(source)
	ir_path = tmp_path / "out.ll"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(_ROOT / "stdlib"),
		 str(src), "--entry", "main::main",
		 "--emit-ir", str(ir_path),
		 "--allow-unsafe"],
		cwd=_ROOT, capture_output=True, text=True, timeout=120,
	)
	assert res.returncode == 0, f"compile failed: {res.stderr[:1500]}"
	return ir_path.read_text()


_DRIFT_MAIN_RE = re.compile(r"define i64 @drift_main\(\) \{(.*?)^}", re.DOTALL | re.MULTILINE)


def _drift_main_body(ir: str) -> str:
	m = _DRIFT_MAIN_RE.search(ir)
	assert m, "could not locate drift_main body"
	return m.group(1)


_INT_FIXTURE = """\
module main;

import std.mem as mem;

pub fn main() nothrow -> Int {
\tunsafe {
\t\tvar slot = mem.maybe_uninit<type Int>();
\t\tmem.maybe_write<type Int>(&mut slot, 42);
\t\tval out = mem.maybe_assume_init_read<type Int>(&mut slot);
\t\treturn out;
\t}
}
"""


def test_maybe_uninit_int_local_emits_zero_init_and_round_trips(tmp_path: Path) -> None:
	"""End-to-end pin for the standalone-local pattern with an `Int`
	payload.  Expectation:

	* `mem.maybe_uninit<type Int>()` lowers (pre-codegen) to a
	  single `ZeroValue` op of type `MaybeUninit<Int>`.
	* The LLVM-side `MaybeUninit<Int>` unwrap collapses the slot
	  type to `i64`; the zero-init becomes `store i64 0, ptr %slot`.
	* The `maybe_assume_init_read` chain reads the value back and
	  zeroes the slot in-place.
	* The whole program round-trips a 42 through the slot and
	  exits with that value.

	Pre-0.31.16 the constructor raised `NotImplementedError`; this
	test would have failed at compile time.
	"""
	ir = _compile_to_ir(tmp_path, _INT_FIXTURE)
	body = _drift_main_body(ir)
	# Pin 1: slot collapses to `i64`-shaped storage.  Confirms the
	# `MaybeUninit<Int>` codegen unwrap is firing; if this fails,
	# the LLVM type for `MaybeUninit<Int>` regressed away from `i64`
	# (touch points in `lang/codegen/llvm/llvm_codegen.py:1052,
	# 7493, 7782, 7862`).
	assert re.search(r"%slot__addr\s*=\s*alloca\s+i64\b", body), (
		f"expected slot to be allocated as `alloca i64` (MaybeUninit<Int> "
		f"unwrap), got body:\n{body[:2000]}"
	)
	# Pin 2: ZeroValue manifests as `add i64 0, 0` in codegen.  The
	# constructor emits one (slot init); `maybe_assume_init_read`'s
	# tombstone emits a second (zero-fill after read).  EXACTLY two.
	# A count of 1 means the constructor lowering regressed back to
	# the pre-0.31.16 stub; a count of 0 means the read tombstone
	# also regressed; > 2 means an extra zero materialization slipped
	# in.  All three are real regressions worth surfacing.
	zero_materializers = re.findall(r"add\s+i64\s+0,\s*0\b", body)
	assert len(zero_materializers) == 2, (
		f"expected EXACTLY 2 `add i64 0, 0` zero-materializers in "
		f"drift_main (one for `mem.maybe_uninit` constructor, one "
		f"for the `mem.maybe_assume_init_read` tombstone).  Got "
		f"{len(zero_materializers)}.  Touch point: "
		f"`hir_to_mir.py::IntrinsicKind.MAYBE_UNINIT` (constructor) "
		f"and `IntrinsicKind.MAYBE_ASSUME_INIT_READ` (tombstone).\n"
		f"Body excerpt:\n{body[:2000]}"
	)
	# Smoke: compile-and-run round-trips the value.
	out_bin = tmp_path / "bin"
	src = tmp_path / "main.drift"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(_ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out_bin),
		 "--allow-unsafe"],
		cwd=_ROOT, capture_output=True, text=True, timeout=120,
	)
	assert res.returncode == 0, f"compile-to-binary failed: {res.stderr[:1500]}"
	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=30)
	assert run.returncode == 42, (
		f"standalone-local round-trip failed: expected exit 42, got "
		f"rc={run.returncode}, stderr={run.stderr[:300]!r}"
	)


_BOX_FIXTURE = """\
module main;

import std.core as core;
import std.mem as mem;

pub struct Box {
\tpub n: Int,
}

implement core.Destructible for Box {
\tpub fn destroy(var self: Box) nothrow -> Void {
\t\treturn;
\t}
}

pub fn main() nothrow -> Int {
\tval b = Box(n = 7);
\tunsafe {
\t\tvar slot = mem.maybe_uninit<type Box>();
\t\tmem.maybe_write<type Box>(&mut slot, b);
\t\tval b2 = mem.maybe_assume_init_read<type Box>(&mut slot);
\t\tcore.drop_value<type Box>(b2);
\t}
\treturn 0;
}
"""


def test_maybe_uninit_local_no_scope_drop_for_maybe_uninit_type(tmp_path: Path) -> None:
	"""`MaybeUninit<T>` is an empty-bodied struct with no
	destructor.  The drop classifier resolves
	`needs_drop(MaybeUninit<T>) = false`, so cleanup_authoring
	emits NO scope-exit destructor for the slot local — even
	though the bytes underneath have been written and read.

	The standing rule is: the user's unsafe contract owns the
	"is the slot occupied?" question; the compiler does not
	speculatively drop on `MaybeUninit<T>`.

	This test pins that contract end-to-end: the only `Box::destroy`
	call in `drift_main` comes from the explicit
	`core.drop_value<type Box>(b2)`.  If a future patch ever made
	the cleanup author drop the slot itself (e.g. by misclassifying
	it through the unwrapped `T` axis instead of the wrapped
	`MaybeUninit<T>` axis), the count would jump to 2."""
	ir = _compile_to_ir(tmp_path, _BOX_FIXTURE)
	body = _drift_main_body(ir)
	destroy_re = re.compile(r'call void @"Box::std.core.Destructible::destroy"')
	count = len(destroy_re.findall(body))
	assert count == 1, (
		f"expected EXACTLY 1 `Box::destroy` call in drift_main "
		f"(from the explicit `core.drop_value(b2)`).  Got {count}.\n"
		f"A count > 1 indicates `MaybeUninit<Box>` got drop-classified "
		f"as if it owned a `Box` — e.g. the empty-struct `needs_drop` "
		f"resolution leaked through the codegen unwrap.  Touch "
		f"points: drop_policy_compute, classify(...) in "
		f"`lang/driftc/stage2/ownership_ledger.py:132-147`, the "
		f"`MaybeUninit` unwrap sites in "
		f"`lang/codegen/llvm/llvm_codegen.py`.\n"
		f"Body excerpt:\n{body[:2000]}"
	)
