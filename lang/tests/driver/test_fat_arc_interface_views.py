# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Phase 1 Stage 3 regressions — fat `Arc<Interface>` representation
boundary.  Active as of ABI 10.

A single `Arc<Concrete>` allocation must be shareable as multiple
`Arc<Interface>` handles, where every handle holds the SAME
control block (and therefore the same strong refcount) but carries
a T-as-I vtable for dispatch.  These tests pin the invariants
listed in `work/fat-arc-interface-views/phase1.md` — see the
"Regression list" section.

The `STAGE3_FAT_ARC_ACTIVE` flag is on; every `Arc<I>` instance
now uses the fat `{ctrl, data, vtable}` layout and is constructed
via `conc.arc(concrete).as_interface<type I>()`.  These
regressions must remain green on the main branch.

Companion negative control: `test_arc_rejects_interface_t.py`
pins the compile-time rejection of direct `conc.arc<T=iface>(...)`.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main
from lang.driftc.parser import stdlib_root


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _compile_and_run(tmp_path: Path, source: str) -> tuple[int, str, str]:
	"""Compile `source` into a binary and run it.

	Returns `(rc, stdout, stderr)` where `rc` is the BINARY exit code
	on success.  A compile failure raises `AssertionError` rather
	than returning — a compile error rc would silently match a
	runtime-exit-code assertion otherwise.
	"""
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
	]
	if root:
		args += ["--stdlib-root", str(root)]
	rc = driftc_main(args)
	assert rc == 0, "driftc compile failed — see captured stderr for diagnostics"
	res = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
	return res.returncode, res.stdout, res.stderr


_HAPPY_PATH_TWO_IFACES = """
module main;

import std.concurrent as conc;

pub interface Greeter {
	fn greet(self: &Self) nothrow -> Int;
}

pub interface Counter {
	fn value(self: &Self) nothrow -> Int;
}

pub struct AppService {
	pub n: Int
}

implement Greeter for AppService {
	pub fn greet(self: &AppService) nothrow -> Int {
		return self.n + 1;
	}
}

implement Counter for AppService {
	pub fn value(self: &AppService) nothrow -> Int {
		return self.n + 2;
	}
}

fn main() nothrow -> Int {
	val svc = conc.arc(AppService(n = 40));
	val g = svc.as_interface<type Greeter>();
	val c = svc.as_interface<type Counter>();
	// g.get().greet() = 41; c.get().value() = 42.
	val sum = g.get().greet() + c.get().value();
	// Expect 83; return a single-byte-friendly value.
	return sum - 41;
}
""".lstrip()


_CHAINED_RVALUE_AS_INTERFACE_CLONE = """
module main;

import std.concurrent as conc;

pub interface Face {
	fn tag(self: &Self) nothrow -> Int;
}

pub struct App {
	pub n: Int
}

implement Face for App {
	pub fn tag(self: &App) nothrow -> Int {
		return self.n;
	}
}

fn main() nothrow -> Int {
	val app = conc.arc(App(n = 42));
	// Chained rvalue receiver: `.as_interface<type Face>()` returns
	// a fresh `Arc<Face>` rvalue that is IMMEDIATELY the receiver
	// of `.clone()`.  Thin Arc already supports this shape (see
	// test_arc_clone_get_chained_rvalue_receiver).  Fat Arc must
	// support it too — chained rvalues are the idiomatic call
	// shape for `as_interface<I>()`.
	val face2 = app.as_interface<type Face>().clone();
	return face2.get().tag();
}
""".lstrip()


_CHAINED_RVALUE_AS_INTERFACE_GET_METHOD = """
module main;

import std.concurrent as conc;

pub interface Face {
	fn tag(self: &Self) nothrow -> Int;
}

pub struct App {
	pub n: Int
}

implement Face for App {
	pub fn tag(self: &App) nothrow -> Int {
		return self.n;
	}
}

fn main() nothrow -> Int {
	val app = conc.arc(App(n = 42));
	// Chained rvalue: `.as_interface<type Face>()` returns a fresh
	// `Arc<Face>` rvalue, `.get()` on that returns a borrowed
	// `&Face` rvalue, and `.tag()` dispatches through the vtable.
	// If the `.get()` path requires an lvalue receiver, this
	// breaks — fat path must materialize the Arc<Face>
	// temporary for the borrow.
	return app.as_interface<type Face>().get().tag();
}
""".lstrip()


_SHARED_MUTATION_VIA_MUTEX = """
module main;

import std.concurrent as conc;
import lang.atomic as atomic;

pub interface Incrementer {
	fn inc(self: &Self) nothrow -> Void;
}

pub interface Reader {
	fn read(self: &Self) nothrow -> Int;
}

pub struct Cell {
	pub v: atomic.AtomicInt
}

implement Incrementer for Cell {
	pub fn inc(self: &Cell) nothrow -> Void {
		val _ = atomic.atomic_fetch_add_int(&self.v, 1, 0);
		return;
	}
}

implement Reader for Cell {
	pub fn read(self: &Cell) nothrow -> Int {
		return atomic.atomic_load_int(&self.v, 0);
	}
}

fn main() nothrow -> Int {
	val c = conc.arc(Cell(v = atomic.atomic_int(10)));
	val inc_view = c.as_interface<type Incrementer>();
	val read_view = c.as_interface<type Reader>();
	inc_view.get().inc();
	inc_view.get().inc();
	// Expect 12 — mutation via one view observed through the other.
	return read_view.get().read();
}
""".lstrip()


_DROP_ORDER_PERMUTATION = """
module main;

import std.concurrent as conc;
import std.core as core;
import lang.atomic as atomic;

// Destructor counter shared across permutations via a
// `conc.Arc<atomic.AtomicInt>`.  Each AppService carries its
// own clone of the Arc so `Destructible::destroy` can touch
// the counter without taking a borrow — borrows can't flow
// through arc's retaining `value: T` param.

pub interface I1 {
	fn m1(self: &Self) nothrow -> Int;
}

pub interface I2 {
	fn m2(self: &Self) nothrow -> Int;
}

pub struct AppService {
	pub n: Int,
	pub counter: conc.Arc<atomic.AtomicInt>
}

implement I1 for AppService {
	pub fn m1(self: &AppService) nothrow -> Int { return self.n; }
}

implement I2 for AppService {
	pub fn m2(self: &AppService) nothrow -> Int { return self.n + 1; }
}

implement core.Destructible for AppService {
	pub fn destroy(var self: AppService) nothrow -> Void {
		val _ = atomic.atomic_fetch_add_int(self.counter.get(), 1, 0);
		return;
	}
}

// Permutation 1: bare scope — Drift drops locals in reverse
// declaration order → v2, v1, arc.
fn run_permutation_arc_v1_v2(counter: &conc.Arc<atomic.AtomicInt>) nothrow -> Void {
	val _ = atomic.atomic_store_int(counter.get(), 0, 0);
	{
		val arc = conc.arc(AppService(n = 7, counter = counter.clone()));
		val v1 = arc.as_interface<type I1>();
		val v2 = arc.as_interface<type I2>();
		val _ = v1.get().m1();
		val _ = v2.get().m2();
	}
	return;
}

// Permutation 2: nested scopes — v1 in inner scope drops before
// v2 and arc drop at outer scope exit.  Different release order
// through the ctrl strong count.
fn run_permutation_nested_scopes(counter: &conc.Arc<atomic.AtomicInt>) nothrow -> Void {
	val _ = atomic.atomic_store_int(counter.get(), 0, 0);
	{
		val arc = conc.arc(AppService(n = 13, counter = counter.clone()));
		val v2 = arc.as_interface<type I2>();
		{
			val v1 = arc.as_interface<type I1>();
			val _ = v1.get().m1();
		}
		// v1 dropped here.  v2 + arc still alive, strong count = 2.
		val _ = v2.get().m2();
	}
	return;
}

// Permutation 3: last face is an interface view — `arc` is
// constructed inside a helper that returns the fat `Arc<I1>`.
// When the helper returns, its local `arc` drops (strong → 1),
// so back in the caller `v1` is the ONLY strong ref; dropping
// `v1` at the end of this function must run drop_thunk.
fn _make_v1_from_svc(svc: AppService) nothrow -> conc.Arc<I1> {
	val arc = conc.arc(move svc);
	return arc.as_interface<type I1>();
}

fn run_permutation_last_face_is_interface(counter: &conc.Arc<atomic.AtomicInt>) nothrow -> Void {
	val _ = atomic.atomic_store_int(counter.get(), 0, 0);
	val v1 = _make_v1_from_svc(AppService(n = 21, counter = counter.clone()));
	val _ = v1.get().m1();
	return;
}

fn main() nothrow -> Int {
	val counter = conc.arc(atomic.atomic_int(0));

	run_permutation_arc_v1_v2(&counter);
	val p1 = atomic.atomic_load_int(counter.get(), 0);
	if p1 != 1 { return 10 + p1; }

	run_permutation_nested_scopes(&counter);
	val p2 = atomic.atomic_load_int(counter.get(), 0);
	if p2 != 1 { return 20 + p2; }

	run_permutation_last_face_is_interface(&counter);
	val p3 = atomic.atomic_load_int(counter.get(), 0);
	if p3 != 1 { return 30 + p3; }

	// All three permutations ran destructor exactly once.
	return 1;
}
""".lstrip()


_NEGATIVE_REQUIRE_FAILURE = """
module main;

import std.concurrent as conc;

pub interface Unrelated {
	fn whatever(self: &Self) nothrow -> Int;
}

pub struct Foo {
	pub n: Int
}

// Foo does NOT implement Unrelated — the `require T is I` clause
// on `Arc<T>.as_interface<I>()` must reject this at typecheck.

fn main() nothrow -> Int {
	val f = conc.arc(Foo(n = 1));
	val bad = f.as_interface<type Unrelated>();
	return 0;
}
""".lstrip()


_CLONE_THROUGH_INTERFACE_VIEW = """
module main;

import std.concurrent as conc;

pub interface Ping {
	fn ping(self: &Self) nothrow -> Int;
}

pub struct Node {
	pub n: Int
}

implement Ping for Node {
	pub fn ping(self: &Node) nothrow -> Int {
		return self.n;
	}
}

fn main() nothrow -> Int {
	val arc = conc.arc(Node(n = 5));
	val v1 = arc.as_interface<type Ping>();
	val v2 = v1.clone();
	// Both views dispatch through the same concrete body and
	// share the ctrl (strong count must still drop to 0 exactly
	// once across v1 + v2 + arc scope exit).
	return v1.get().ping() + v2.get().ping() - 5;
}
""".lstrip()


def test_happy_path_two_interfaces_dispatch(tmp_path: Path) -> None:
	"""Regression #1: one concrete impls two interfaces; build Arc<T>,
	derive Arc<I1> + Arc<I2>, dispatch through each view and confirm
	correct values."""
	rc, stdout, stderr = _compile_and_run(tmp_path, _HAPPY_PATH_TWO_IFACES)
	assert rc == 42, (
		f"expected rc=42 (greet=41 + value=42 - 41), got rc={rc}\n"
		f"stdout={stdout!r} stderr={stderr!r}"
	)


def test_shared_mutation_across_interface_views(tmp_path: Path) -> None:
	"""Regression #2: mutation via one Arc<I> view observed through
	another Arc<I> view.  Verifies single underlying payload."""
	rc, stdout, stderr = _compile_and_run(tmp_path, _SHARED_MUTATION_VIA_MUTEX)
	assert rc == 12, (
		f"expected rc=12 (10 + 2 increments), got rc={rc}\n"
		f"stdout={stdout!r} stderr={stderr!r}"
	)


def test_drop_order_destructor_runs_exactly_once(tmp_path: Path) -> None:
	"""Regression #3: `Destructible::destroy` must fire EXACTLY ONCE
	for the shared `AppService` no matter which face (thin `arc`
	or either interface view) holds the last strong reference at
	drop time.

	The embedded Drift program runs three distinct permutations and
	asserts the drop counter is 1 after each:

	1. Bare scope, reverse-declaration order → `v2`, `v1`, `arc`
	   drop in that order when the block exits.
	2. Nested scopes → `v1` drops in an inner block while `v2`
	   and `arc` stay alive; counter stays at 0 until outer scope.
	3. Last face is an interface view → `arc` goes out of scope
	   early (block-expression result binds `v1` and drops `arc`);
	   `v1`'s drop must run `drop_thunk` because it holds the
	   final strong reference.

	A permutation that double-drops returns `10*N + count` for
	permutation N so a test failure pinpoints which shape broke."""
	rc, stdout, stderr = _compile_and_run(tmp_path, _DROP_ORDER_PERMUTATION)
	assert rc == 1, (
		f"expected destructor to fire exactly once across all three "
		f"permutations (rc=1), got rc={rc}\n"
		f"stdout={stdout!r} stderr={stderr!r}"
	)


def test_as_interface_rejected_when_require_fails(tmp_path: Path) -> None:
	"""Regression #4: concrete that does NOT implement the target
	interface must be rejected at typecheck with the `require T is I`
	unsatisfied diagnostic.  No runtime artifact.

	This check already passes under Stage 1 compile-time gating — it
	is kept here so the full Stage 3 regression set is colocated."""
	mod_root = tmp_path / "mods"
	main_src = mod_root / "main" / "main.drift"
	_write_file(main_src, _NEGATIVE_REQUIRE_FAILURE)
	exe = tmp_path / "out"
	root = stdlib_root()
	args = [
		"-M", str(mod_root),
		str(main_src),
		"-o", str(exe),
		"--dev",
	]
	if root:
		args += ["--stdlib-root", str(root)]
	rc = driftc_main(args)
	assert rc != 0, "as_interface with unsatisfied require must be rejected"


def test_as_interface_chained_rvalue_clone(tmp_path: Path) -> None:
	"""Fat rvalue receiver — parallel to the thin-Arc regression
	`test_arc_clone_get_chained_rvalue_receiver`.  The shape
	`app.as_interface<type Face>().clone()` produces a chain where
	the `.clone()` receiver is the immediate rvalue `Arc<Face>`
	from `.as_interface<I>()`.  Slice 3 gate: this must compile
	and run alongside layout activation, not as a post-activation
	follow-up.  Returns 42 when wired correctly."""
	rc, stdout, stderr = _compile_and_run(tmp_path, _CHAINED_RVALUE_AS_INTERFACE_CLONE)
	assert rc == 42, (
		f"chained rvalue .as_interface<I>().clone() did not dispatch: "
		f"rc={rc} stdout={stdout!r} stderr={stderr!r}"
	)


def test_as_interface_chained_rvalue_get_method(tmp_path: Path) -> None:
	"""Fat rvalue receiver — the hot-path shape for logger-style
	use: `app.as_interface<type Face>().get().method()`.
	The `.get()` receiver is an immediate rvalue `Arc<Face>`,
	and the `.method()` receiver is a borrowed `&Face` rvalue.
	Slice 3 gate: idiomatic user shape, must work on activation."""
	rc, stdout, stderr = _compile_and_run(tmp_path, _CHAINED_RVALUE_AS_INTERFACE_GET_METHOD)
	assert rc == 42, (
		f"chained rvalue .as_interface<I>().get().method() did not "
		f"dispatch: rc={rc} stdout={stdout!r} stderr={stderr!r}"
	)


def test_clone_through_interface_view_preserves_dispatch(tmp_path: Path) -> None:
	"""Regression #5: `Arc<I>.clone()` produces a second `Arc<I>`
	sharing the same ctrl; both dispatch correctly."""
	rc, stdout, stderr = _compile_and_run(tmp_path, _CLONE_THROUGH_INTERFACE_VIEW)
	assert rc == 5, (
		f"expected rc=5 (ping + ping - 5), got rc={rc}\n"
		f"stdout={stdout!r} stderr={stderr!r}"
	)


def test_std_log_resolver_api_shape_compiles(tmp_path: Path) -> None:
	"""Regression #6 (COMPILE-LEVEL SMOKE ONLY — not the std.log
	runtime integration pin): a minimal program that constructs
	`Arc<MyResolver>`, coerces to `Arc<log.ContextResolver>` via
	`as_interface<I>()`, and threads it through
	`log.config_builder().context_resolver(...)` must compile
	successfully against the fat Arc<I> representation.

	**This test verifies the API shape only.**  It does NOT invoke
	any logging or exercise the resolver at runtime.  The actual
	std.log runtime integration pin is the existing e2e fixture
	`lang/tests/codegen/e2e/std_log_resolver_active/` — that
	fixture runs the binary, emits log lines, and asserts the
	resolver-populated attrs land in the output.  Step 6 of the
	Stage 3 plan keeps the std_log_resolver_active e2e fixture in
	the required gate precisely because this smoke test is not a
	substitute for it."""
	source = """
module main;

import std.log as log;
import std.concurrent as conc;

pub struct MyResolver {
	pub tag: Int
}

implement log.ContextResolver for MyResolver {
	pub fn resolve(self: &MyResolver) nothrow -> Optional<&log.LogContext> {
		return Optional<&log.LogContext>::None();
	}
}

fn main() nothrow -> Int {
	val r = conc.arc(MyResolver(tag = 7));
	val view = r.as_interface<type log.ContextResolver>();
	var b = log.config_builder();
	b.context_resolver(view);
	return 0;
}
""".lstrip()
	rc, stdout, stderr = _compile_and_run(tmp_path, source)
	assert rc == 0, (
		f"std.log Arc<ContextResolver> integration failed: rc={rc}\n"
		f"stdout={stdout!r} stderr={stderr!r}"
	)


def test_fat_arc_destroy_ir_shape(tmp_path: Path) -> None:
	"""IR-level negative + positive pin for the fat Arc<I> destroy path.

	After activation, fat `Arc<I>` destruction MUST route through the
	per-I synthesized wrapper + non-generic ctrl helper, not through
	the thin `_arc_destroy_impl<T>` template:

	- **positive**: at least one `_arc_fat_destroy_wrapper__<N>` must
	  be defined and referenced; `_arc_fat_drop_via_ctrl` must be
	  both defined and called.
	- **negative**: no `_arc_destroy_impl__inst__<hash>` function may
	  take a FAT Arc struct (the `{ptr, ptr, ptr}` layout) as its
	  parameter.  Thin concrete-T Arc instances keep their own thin
	  helpers — the rule is specifically about fat-layout instances.

	The fixture uses `_HAPPY_PATH_TWO_IFACES` because it produces
	two fat `Arc<I>` instances (Greeter, Counter) plus one thin
	`Arc<AppService>` — so both sides of the distinction appear in
	one IR module and the negative check has real discriminating
	power.
	"""
	mod_root = tmp_path / "mods"
	main_src = mod_root / "main" / "main.drift"
	_write_file(main_src, _HAPPY_PATH_TWO_IFACES)
	exe = tmp_path / "out"
	root = stdlib_root()
	args = [
		"-M", str(mod_root),
		str(main_src),
		"-o", str(exe),
		"--dev",
	]
	if root:
		args += ["--stdlib-root", str(root)]
	rc = driftc_main(args)
	assert rc == 0, "driftc compile failed — see captured stderr"

	ir_path = exe.with_suffix(".ll")
	assert ir_path.exists(), f"expected IR at {ir_path}"
	ir = ir_path.read_text(encoding="utf-8")

	# Extract fat Arc struct hashes — type lines matching
	# `%Struct_std_2Econcurrent_Arc_<hash> = type { ptr, ptr, ptr }`.
	fat_re = re.compile(
		r"^%Struct_std_2Econcurrent_Arc_([0-9a-f]+) = type \{ ptr, ptr, ptr \}$",
		re.MULTILINE,
	)
	fat_hashes = set(fat_re.findall(ir))
	assert fat_hashes, (
		"expected at least one fat Arc struct (`{ ptr, ptr, ptr }`) in "
		"IR — fat layout activation appears to be off or unreachable"
	)

	# Positive: at least one synthesized fat-destroy wrapper must be
	# defined.  Symbol name pattern is `_arc_fat_destroy_wrapper__<N>`
	# where N is the fat Arc<I> inst TypeId (an integer).
	wrapper_def_re = re.compile(
		r'^define [^\n]*@"std\.concurrent::_arc_fat_destroy_wrapper__\d+"',
		re.MULTILINE,
	)
	wrapper_defs = wrapper_def_re.findall(ir)
	assert wrapper_defs, (
		"no `_arc_fat_destroy_wrapper__<N>` definition in IR — the "
		"fat Arc<I> destructor synthesis pass did not fire or did not "
		"install the wrapper"
	)
	# And the wrapper must actually be called at some scope-drop site
	# — otherwise the destructor is dead code and AppService::destroy
	# would never run.
	assert 'call void @"std.concurrent::_arc_fat_destroy_wrapper__' in ir, (
		"no call to `_arc_fat_destroy_wrapper__<N>` in IR — scope-drop "
		"of fat Arc<I> is not dispatching through the wrapper"
	)

	# `_arc_fat_drop_via_ctrl` must be defined AND called (the wrapper
	# calls it).
	assert '@"std.concurrent::_arc_fat_drop_via_ctrl"' in ir, (
		"`_arc_fat_drop_via_ctrl` symbol missing from IR — the ctrl-only "
		"runtime helper is not linked"
	)
	assert 'call void @"std.concurrent::_arc_fat_drop_via_ctrl"(' in ir, (
		"`_arc_fat_drop_via_ctrl` is defined but never called — the "
		"wrapper is not forwarding through the ctrl-only drop path"
	)

	# `_arc_fat_bump_strong_via_ctrl` must have a prototype visible at
	# the call site — either a `declare` (package-consumer build where
	# the helper's body lives in an upstream module) OR a `define`
	# (single-module / dev build where stdlib compiles inline; LLVM
	# rejects both in one module even with identical prototypes).
	# And the call MUST use the `_llvm_fn_sym` spelling — same
	# escaping as every other Drift symbol.
	_bump_declared = 'declare void @"std.concurrent::_arc_fat_bump_strong_via_ctrl"(ptr)' in ir
	_bump_defined = 'define void @"std.concurrent::_arc_fat_bump_strong_via_ctrl"(' in ir
	assert _bump_declared or _bump_defined, (
		"`_arc_fat_bump_strong_via_ctrl` has neither a declare nor a "
		"define in IR — the Stage 3 fat-Arc bump helper is not linked"
	)
	assert 'call void @"std.concurrent::_arc_fat_bump_strong_via_ctrl"(ptr' in ir, (
		"`_arc_fat_bump_strong_via_ctrl` linked but never called — "
		"`ArcAsInterface` lowering is not emitting the strong-bump call"
	)

	# Negative: for EVERY fat Arc<I> instance, no thin
	# `_arc_destroy_impl__inst__<hash>` function may take that Arc's
	# struct as its parameter.  Pattern match the define line and
	# pull the parameter struct name.
	destroy_impl_re = re.compile(
		r'^define [^\n]*@"std\.concurrent::_arc_destroy_impl__inst__'
		r'[0-9a-f]+"\(%Struct_std_2Econcurrent_Arc_([0-9a-f]+) ',
		re.MULTILINE,
	)
	destroy_impl_struct_hashes = set(destroy_impl_re.findall(ir))
	leaked_fat = fat_hashes & destroy_impl_struct_hashes
	assert not leaked_fat, (
		f"thin `_arc_destroy_impl__inst__<hash>` leaked for {len(leaked_fat)} "
		f"fat Arc<I> instance(s) ({sorted(leaked_fat)}) — the scan-time + "
		f"helper-instantiation skips for `is_arc_fat_layout_instance` "
		f"failed, and a structurally-invalid thin helper was emitted"
	)
