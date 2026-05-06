"""
Top-surface driftc → LLVM IR → clang execution smoke test.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from lang.driftc.checker import FnSignature
from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
from lang.driftc import stage1 as H

BUILD_ROOT = Path("build/tests/lang/driftc_codegen_scalar")


def _run_ir_with_clang(ir: str) -> int:
	"""Compile the provided LLVM IR with clang and return the process exit code."""
	clang = shutil.which("clang")
	if clang is None:
		raise RuntimeError("clang not available")

	BUILD_ROOT.mkdir(parents=True, exist_ok=True)
	work_dir = Path(tempfile.mkdtemp(prefix="driftc-codegen-", dir=BUILD_ROOT))
	try:
		ir_path = work_dir / "ir.ll"
		bin_path = work_dir / "a.out"
		ir_path.write_text(ir)

		compile_res = subprocess.run(
			[clang, "-x", "ir", str(ir_path), "-o", str(bin_path)],
			capture_output=True,
			text=True,
		)
		if compile_res.returncode != 0:
			raise RuntimeError(f"clang failed: {compile_res.stderr}")

		run_res = subprocess.run(
			[str(bin_path)],
			capture_output=True,
			text=True,
		)
		return run_res.returncode
	finally:
		shutil.rmtree(work_dir, ignore_errors=True)


def _extract_llvm_function(ir: str, name: str) -> str:
	lines = ir.splitlines()
	out: list[str] = []
	in_fn = False
	for line in lines:
		if line.startswith("define ") and f"@\"{name}\"" in line or line.startswith("define ") and f"@{name}" in line:
			in_fn = True
		if in_fn:
			out.append(line)
			if line.strip() == "}":
				break
	return "\n".join(out)


def test_driftc_codegen_scalar_main():
	"""
	Full pipeline smoke: HIR -> MIR -> SSA -> LLVM -> clang.
	fn drift_main() -> Int { return 42; } should exit with code 42.
	"""
	func_hirs = {
		"drift_main": H.HBlock(statements=[H.HReturn(value=H.HLiteralInt(value=42))])
	}
	signatures = {"drift_main": FnSignature(name="drift_main", return_type="Int", declared_can_throw=False)}

	ir, _ = compile_to_llvm_ir_for_tests(func_hirs=func_hirs, signatures=signatures, entry="drift_main", root_vt=False)
	exit_code = _run_ir_with_clang(ir)
	assert exit_code == 42


def test_driftc_codegen_can_throw_callee_ok():
	"""
	Full pipeline smoke with a can-throw callee returning 1.

	Surface language uses `-> T` even for can-throw functions; the can-throw
	ABI carrier (FnResult) is internal. `drift_main` stays non-can-throw and
	handles failures locally via a try/catch expression, returning the ok value
	on success.
	"""
	func_hirs = {
		"callee": H.HBlock(
			statements=[H.HReturn(value=H.HLiteralInt(value=1))]
		),
		"drift_main": H.HBlock(
			statements=[
				H.HReturn(
					value=H.HTryExpr(
						attempt=H.HCall(fn=H.HVar(name="callee"), args=[]),
						arms=[
							H.HTryExprArm(
								event_fqn=None,
								binder=None,
								block=H.HBlock(statements=[]),
								result=H.HLiteralInt(value=0),
							)
						],
					)
				)
			]
		),
	}
	signatures = {
		"callee": FnSignature(name="callee", return_type="Int", declared_can_throw=True),
		"drift_main": FnSignature(name="drift_main", return_type="Int", declared_can_throw=False),
	}

	ir, _ = compile_to_llvm_ir_for_tests(func_hirs=func_hirs, signatures=signatures, entry="drift_main", root_vt=False)
	exit_code = _run_ir_with_clang(ir)
	assert exit_code == 1


def test_driftc_codegen_callback_indirect(tmp_path: Path):
	core_src = tmp_path / "core.drift"
	core_src.write_text(
		"""
module std.core;

export {
	Callback1,
	callback1
};

pub trait Copy {
}

implement Copy for Int {
}

pub trait Diagnostic {
}

pub interface Callback1<A, R> {
	fn call(self: &Self, a: A) nothrow -> R;
}

@intrinsic pub fn callback1<F, A, R>(f: F) -> Callback1<A, R>;
"""
	)
	src = tmp_path / "main.drift"
	src.write_text(
		"""
module main;

import std.core as core;

fn add1(x: Int) nothrow -> Int {
	return x + 1;
}

fn drift_main() nothrow -> Int {
	var cb = core.callback1(add1);
	return cb.call(41);
}
"""
	)
	paths = sorted(tmp_path.rglob("*.drift"))
	modules, type_table, _exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[tmp_path],
		stdlib_root=None,
	)
	assert diagnostics == []
	func_hirs, signatures, _fn_ids_by_name = flatten_modules(modules)
	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		entry="drift_main",
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		prelude_enabled=False,
		root_vt=False,
	)
	assert checked.diagnostics == []
	# With opaque pointers the GEP pattern is: getelementptr inbounds ptr, ptr %vtable, i32 N
	vtable_lines = [line for line in ir.splitlines() if "getelementptr inbounds ptr, ptr" in line]
	assert any("i32 1" in line for line in vtable_lines)
	exit_code = _run_ir_with_clang(ir)
	assert exit_code == 42


def test_driftc_codegen_callback_indirect_zero_and_two_args(tmp_path: Path):
	core_src = tmp_path / "core.drift"
	core_src.write_text(
		"""
module std.core;

export {
	Callback0,
	Callback2,
	callback0,
	callback2
};

pub trait Copy {
}

implement Copy for Int {
}

pub trait Diagnostic {
}

pub interface Callback0<R> {
	fn call(self: &Self) nothrow -> R;
}

pub interface Callback2<A, B, R> {
	fn call(self: &Self, a: A, b: B) nothrow -> R;
}

@intrinsic pub fn callback0<F, R>(f: F) -> Callback0<R>;
@intrinsic pub fn callback2<F, A, B, R>(f: F) -> Callback2<A, B, R>;
"""
	)
	src = tmp_path / "main.drift"
	src.write_text(
		"""
module main;

import std.core as core;

fn zero() nothrow -> Int {
	return 40;
}

fn add2(a: Int, b: Int) nothrow -> Int {
	return a + b;
}

fn drift_main() nothrow -> Int {
	var c0 = core.callback0(zero);
	var c2 = core.callback2(add2);
	return c0.call() + c2.call(1, 1);
}
"""
	)
	paths = sorted(tmp_path.rglob("*.drift"))
	modules, type_table, _exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[tmp_path],
		stdlib_root=None,
	)
	assert diagnostics == []
	func_hirs, signatures, _fn_ids_by_name = flatten_modules(modules)
	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		entry="drift_main",
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		prelude_enabled=False,
		root_vt=False,
	)
	assert checked.diagnostics == []
	exit_code = _run_ir_with_clang(ir)
	assert exit_code == 42


def test_driftc_codegen_callback_arc_mutex_stub(tmp_path: Path):
	core_src = tmp_path / "core.drift"
	core_src.write_text(
		"""
module std.core;

export {
	Copy,
	Destructible,
	Diagnostic,
	Fn1,
	Callback1,
	callback1
};

pub trait Copy {
}

pub trait Destructible {
	fn destroy(self: Self) nothrow -> Void;
}

pub trait Diagnostic {
	fn to_json_text(self: &Self) nothrow -> String;
}

pub trait Fn1<A, R> {
	fn call(self: &Self, a: A) -> R;
}

pub interface Callback1<A, R> {
	fn call(self: &Self, a: A) nothrow -> R;
}

@intrinsic pub fn callback1<F, A, R>(f: F) -> Callback1<A, R> require F is Fn1<A, R>;

implement Copy for Int { }
implement Copy for Bool { }
implement Copy for Void { }
"""
	)
	mem_src = tmp_path / "mem.drift"
	mem_src.write_text(
		"""
module std.mem;

export { Ptr, RawBuffer, alloc_uninit, dealloc, ptr_at_ref, ptr_at_mut, write, read };

import std.core as core;

pub struct Ptr<T> {
	addr: Int
}

pub struct RawBuffer<T> {
	ptr: Ptr<Byte>
	cap: Int
}

@intrinsic pub unsafe fn alloc_uninit<T>(cap: Int) nothrow -> RawBuffer<T>;
@intrinsic pub unsafe fn dealloc<T>(buf: RawBuffer<T>) nothrow -> Void;
@intrinsic pub unsafe fn ptr_at_ref<T>(buf: &RawBuffer<T>, i: Int) nothrow -> &T;
@intrinsic pub unsafe fn ptr_at_mut<T>(buf: &mut RawBuffer<T>, i: Int) nothrow -> &mut T;
@intrinsic pub unsafe fn write<T>(buf: &mut RawBuffer<T>, i: Int, v: T) nothrow -> Void;
@intrinsic pub unsafe fn read<T>(buf: &mut RawBuffer<T>, i: Int) nothrow -> T;

implement<T> core.Copy for Ptr<T> { }
implement<T> core.Copy for RawBuffer<T> { }
"""
	)
	conc_src = tmp_path / "concurrent.drift"
	conc_src.write_text(
		"""
module std.concurrent;

export { Arc, Mutex, MutexGuard, StateMachine, arc, mutex };

import std.core as core;
import std.mem as mem;

use trait core.Destructible;

pub struct ArcInner<T> {
	refcnt: Int
	value: T
}

pub struct Arc<T> {
	buf: mem.RawBuffer<ArcInner<T>>
}

pub struct Mutex<T> {
	buf: mem.RawBuffer<T>
}

pub struct MutexGuard<T> {
	buf: mem.RawBuffer<T>
}

pub struct StateMachine {
	pub count: Int
}

implement StateMachine {
	pub fn on_x(self: &mut StateMachine, v: Int) nothrow -> Void {
		self.count = self.count + v;
	}
}

pub fn arc<T>(value: T) nothrow -> Arc<T> {
	unsafe {
		var buf = mem.alloc_uninit<type ArcInner<T>>(1);
		mem.write<type ArcInner<T>>(&mut buf, 0, ArcInner(refcnt = 1, value = value));
		return Arc(buf = buf);
	}
}

implement<T> Arc<T> {
	pub fn clone(self: &Arc<T>) nothrow -> Arc<T> {
		unsafe {
			var buf = self.buf;
			val inner = mem.read<type ArcInner<T>>(&mut buf, 0);
			mem.write<type ArcInner<T>>(&mut buf, 0, ArcInner(refcnt = inner.refcnt + 1, value = inner.value));
			return Arc(buf = buf);
		}
	}

	pub fn as_mut(self: &mut Arc<T>) nothrow -> &mut T {
		unsafe {
			var buf = self.buf;
			val inner = mem.ptr_at_mut<type ArcInner<T>>(&mut buf, 0);
			return &mut inner.value;
		}
	}
}

pub fn mutex<T>(value: T) nothrow -> Mutex<T> {
	unsafe {
		var buf = mem.alloc_uninit<type T>(1);
		mem.write<type T>(&mut buf, 0, value);
		return Mutex(buf = buf);
	}
}

implement<T> Mutex<T> {
	pub fn lock(self: &Mutex<T>) nothrow -> MutexGuard<T> {
		return MutexGuard(buf = self.buf);
	}
}

implement<T> MutexGuard<T> {
	pub fn get_mut(self: &mut MutexGuard<T>) nothrow -> &mut T {
		unsafe { return mem.ptr_at_mut<type T>(&mut self.buf, 0); }
	}
}

implement core.Destructible for Mutex<StateMachine> {
	pub fn destroy(self: Mutex<StateMachine>) nothrow -> Void {
		unsafe { mem.dealloc<type StateMachine>(self.buf); }
	}
}

implement core.Destructible for Arc<Mutex<StateMachine>> {
	pub fn destroy(self: Arc<Mutex<StateMachine>>) nothrow -> Void {
		unsafe {
			var buf = self.buf;
			val inner = mem.read<type ArcInner<Mutex<StateMachine>>>( &mut buf, 0);
			val rc = inner.refcnt - 1;
			if rc == 0 {
				mem.dealloc<type ArcInner<Mutex<StateMachine>>>(buf);
			} else {
				mem.write<type ArcInner<Mutex<StateMachine>>>(&mut buf, 0, ArcInner(refcnt = rc, value = inner.value));
			}
		}
	}
}
"""
	)
	src = tmp_path / "main.drift"
	src.write_text(
		"""
module main;

import std.core as core;
import std.concurrent as conc;

struct Event {
	v: Int
	state: conc.Arc<conc.Mutex<conc.StateMachine>>
}

fn on_x(e: Event) nothrow -> Void {
	try {
		var s = e.state;
		var sm_ref = s.as_mut();
		var g = sm_ref.lock();
		var m = g.get_mut();
		m.on_x(e.v);
	} catch err {
	}
}

fn drift_main() nothrow -> Int {
	var m: conc.Mutex<conc.StateMachine> = conc.mutex(conc.StateMachine(count = 0));
	var sm: conc.Arc<conc.Mutex<conc.StateMachine>> = conc.arc(m);
	var e = Event(v = 41, state = sm.clone());
	var cb = core.callback1(on_x);
	cb.call(move e);
	var sm_ref2 = sm.as_mut();
	var g2 = sm_ref2.lock();
	var m2 = g2.get_mut();
	return m2.count;
}
"""
	)
	paths = sorted(tmp_path.rglob("*.drift"))
	modules, type_table, _exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[tmp_path],
		stdlib_root=None,
	)
	assert diagnostics == []
	func_hirs, signatures, _fn_ids_by_name = flatten_modules(modules)
	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		entry="drift_main",
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		prelude_enabled=False,
		root_vt=False,
	)
	assert checked.diagnostics == []
	assert "@drift_alloc_array" in ir
	assert "@drift_bounds_check" in ir


def test_driftc_codegen_callback_stored_in_struct(tmp_path: Path):
	core_src = tmp_path / "core.drift"
	core_src.write_text(
		"""
module std.core;

export {
	Callback1,
	callback1
};

pub trait Copy {
}

implement Copy for Int {
}

pub trait Diagnostic {
}

pub interface Callback1<A, R> {
	fn call(self: &Self, a: A) nothrow -> R;
}

@intrinsic pub fn callback1<F, A, R>(f: F) -> Callback1<A, R>;
"""
	)
	src = tmp_path / "main.drift"
	src.write_text(
		"""
module main;

import std.core as core;

struct Holder {
	cb: core.Callback1<Int, Int>
}

fn add1(x: Int) nothrow -> Int {
	return x + 1;
}

fn drift_main() nothrow -> Int {
	var h = Holder(cb = core.callback1(add1));
	return h.cb.call(41);
}
"""
	)
	paths = sorted(tmp_path.rglob("*.drift"))
	modules, type_table, _exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[tmp_path],
		stdlib_root=None,
	)
	assert diagnostics == []
	func_hirs, signatures, _fn_ids_by_name = flatten_modules(modules)
	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		entry="drift_main",
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		prelude_enabled=False,
		root_vt=False,
	)
	assert checked.diagnostics == []
	main_ir = _extract_llvm_function(ir, "drift_main")
	# With opaque pointers: bitcasts are eliminated, typed pointers become ptr.
	needles = [
		"extractvalue %DriftIface",
		"getelementptr inbounds ptr, ptr",
		"load ptr",
		"call",
	]
	pos = -1
	for needle in needles:
		next_pos = main_ir.find(needle, pos + 1)
		assert next_pos != -1, f"needle {needle!r} not found in main_ir"
		pos = next_pos
	assert "(ptr " in main_ir
	assert "call i64 @add1" not in main_ir
	exit_code = _run_ir_with_clang(ir)
	assert exit_code == 42


def test_driftc_codegen_callback_param(tmp_path: Path):
	core_src = tmp_path / "core.drift"
	core_src.write_text(
		"""
module std.core;

export {
	Callback1,
	callback1
};

pub trait Copy {
}

implement Copy for Int {
}

pub trait Diagnostic {
}

pub interface Callback1<A, R> {
	fn call(self: &Self, a: A) nothrow -> R;
}

@intrinsic pub fn callback1<F, A, R>(f: F) -> Callback1<A, R>;
"""
	)
	src = tmp_path / "main.drift"
	src.write_text(
		"""
module main;

import std.core as core;

fn add1(x: Int) nothrow -> Int {
	return x + 1;
}

fn apply(cb: core.Callback1<Int, Int>, v: Int) nothrow -> Int {
	return cb.call(v);
}

fn drift_main() nothrow -> Int {
	return apply(core.callback1(add1), 41);
}
"""
	)
	paths = sorted(tmp_path.rglob("*.drift"))
	modules, type_table, _exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[tmp_path],
		stdlib_root=None,
	)
	assert diagnostics == []
	func_hirs, signatures, _fn_ids_by_name = flatten_modules(modules)
	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		entry="drift_main",
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		prelude_enabled=False,
		root_vt=False,
	)
	assert checked.diagnostics == []
	# With opaque pointers: bitcasts are eliminated, typed pointers become ptr.
	needles = [
		"extractvalue %DriftIface",
		"getelementptr inbounds ptr, ptr",
		"load ptr",
		"call",
	]
	pos = -1
	for needle in needles:
		next_pos = ir.find(needle, pos + 1)
		assert next_pos != -1, f"needle {needle!r} not found in ir"
		pos = next_pos
	assert "(ptr " in ir
	apply_ir = _extract_llvm_function(ir, "apply")
	assert "call i64 @add1" not in apply_ir
	exit_code = _run_ir_with_clang(ir)
	assert exit_code == 42


def test_driftc_codegen_callback_param_zero_and_two_args(tmp_path: Path):
	core_src = tmp_path / "core.drift"
	core_src.write_text(
		"""
module std.core;

export {
	Callback0,
	Callback2,
	callback0,
	callback2
};

pub trait Copy {
}

implement Copy for Int {
}

pub trait Diagnostic {
}

pub interface Callback0<R> {
	fn call(self: &Self) nothrow -> R;
}

pub interface Callback2<A, B, R> {
	fn call(self: &Self, a: A, b: B) nothrow -> R;
}

@intrinsic pub fn callback0<F, R>(f: F) -> Callback0<R>;
@intrinsic pub fn callback2<F, A, B, R>(f: F) -> Callback2<A, B, R>;
"""
	)
	src = tmp_path / "main.drift"
	src.write_text(
		"""
module main;

import std.core as core;

fn zero() nothrow -> Int {
	return 40;
}

fn add2(a: Int, b: Int) nothrow -> Int {
	return a + b;
}

fn apply0(cb: core.Callback0<Int>) nothrow -> Int {
	return cb.call();
}

fn apply2(cb: core.Callback2<Int, Int, Int>, v: Int) nothrow -> Int {
	return cb.call(v, 1);
}

fn drift_main() nothrow -> Int {
	return apply0(core.callback0(zero)) + apply2(core.callback2(add2), 1);
}
"""
	)
	paths = sorted(tmp_path.rglob("*.drift"))
	modules, type_table, _exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[tmp_path],
		stdlib_root=None,
	)
	assert diagnostics == []
	func_hirs, signatures, _fn_ids_by_name = flatten_modules(modules)
	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		entry="drift_main",
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		prelude_enabled=False,
		root_vt=False,
	)
	assert checked.diagnostics == []
	exit_code = _run_ir_with_clang(ir)
	assert exit_code == 42
