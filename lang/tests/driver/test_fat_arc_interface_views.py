# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Phase 1 Stage 3 regression: fat `Arc<Interface>` representation
boundary.

A single `Arc<Concrete>` allocation must be shareable as multiple
`Arc<Interface>` handles, where every handle holds the SAME
control block (and therefore the same strong refcount) but carries
a T-as-I vtable for dispatch.  The six tests below pin the
invariants listed in
`work/fat-arc-interface-views/phase1.md` — see the "Regression
list" section.

**These tests pre-date the Stage 3 implementation.  Every test
that calls `as_interface<I>()` is expected to fail today with the
Stage 2 placeholder assertion:**

    Arc.as_interface<I>() runtime lowering is not yet implemented
    (Stage 3); callsite=<N>

As Stage 3 lands — layout specialization + fat-T lowerings +
ABI 10 — the tests flip to green one by one.  Test #4 (negative
`require` case) already passes under Stage 1 compile-time gating.
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
import std.sync as sync;

pub interface Incrementer {
	fn inc(self: &Self) nothrow -> Void;
}

pub interface Reader {
	fn read(self: &Self) nothrow -> Int;
}

pub struct Cell {
	pub v: sync.AtomicInt
}

implement Incrementer for Cell {
	pub fn inc(self: &Cell) nothrow -> Void {
		val _ = sync.atomic_fetch_add_int(&self.v, 1, 0);
		return;
	}
}

implement Reader for Cell {
	pub fn read(self: &Cell) nothrow -> Int {
		return sync.atomic_load_int(&self.v, 0);
	}
}

fn main() nothrow -> Int {
	val c = conc.arc(Cell(v = sync.atomic_int(10)));
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
import std.sync as sync;

// Module-global destructor counter.  Incremented exactly once by
// `AppService::destroy`.  `main` checks the counter after each
// permutation has scope-exited.
var DROP_COUNTER: sync.AtomicInt = sync.atomic_int(0);

pub interface I1 {
	fn m1(self: &Self) nothrow -> Int;
}

pub interface I2 {
	fn m2(self: &Self) nothrow -> Int;
}

pub struct AppService {
	pub n: Int
}

implement I1 for AppService {
	pub fn m1(self: &AppService) nothrow -> Int { return self.n; }
}

implement I2 for AppService {
	pub fn m2(self: &AppService) nothrow -> Int { return self.n + 1; }
}

implement core.Destructible for AppService {
	pub fn destroy(var self: AppService) nothrow -> Void {
		val _ = sync.atomic_fetch_add_int(&DROP_COUNTER, 1, 0);
		return;
	}
}

// Permutation 1: bare scope — Drift drops locals in reverse
// declaration order → v2, v1, arc.
fn run_permutation_arc_v1_v2() nothrow -> Void {
	val _ = sync.atomic_store_int(&DROP_COUNTER, 0, 0);
	{
		val arc = conc.arc(AppService(n = 7));
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
fn run_permutation_nested_scopes() nothrow -> Void {
	val _ = sync.atomic_store_int(&DROP_COUNTER, 0, 0);
	{
		val arc = conc.arc(AppService(n = 13));
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

// Permutation 3: last face is an interface view (arc dropped
// first via early re-bind into an immediately-discarded temp),
// proving that the drop_thunk fires from whatever face holds
// the last strong count.
fn run_permutation_last_face_is_interface() nothrow -> Void {
	val _ = sync.atomic_store_int(&DROP_COUNTER, 0, 0);
	val v1 = {
		val arc = conc.arc(AppService(n = 21));
		arc.as_interface<type I1>()
	};
	// `arc` is now out of scope; `v1` holds the ONLY strong
	// reference to the ArcBox.  Dropping `v1` at the end of
	// this function must run drop_thunk.
	val _ = v1.get().m1();
	return;
}

fn main() nothrow -> Int {
	run_permutation_arc_v1_v2();
	val p1 = sync.atomic_load_int(&DROP_COUNTER, 0);
	if p1 != 1 { return 10 + p1; }

	run_permutation_nested_scopes();
	val p2 = sync.atomic_load_int(&DROP_COUNTER, 0);
	if p2 != 1 { return 20 + p2; }

	run_permutation_last_face_is_interface();
	val p3 = sync.atomic_load_int(&DROP_COUNTER, 0);
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
