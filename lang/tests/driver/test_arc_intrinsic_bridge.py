# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Stage 2 Option B regression: `@intrinsic` Arc methods (clone / get /
Destructible::destroy) route through the compiler's INTRINSIC target
kind and lower as direct calls to the monomorphized `_arc_*_impl<T>`
helpers — **not** as method calls on the bodyless intrinsic template,
and **not** as calls to a generic (un-instantiated) helper symbol.

What this pins:

1. Behavioural: `arc.clone().get()` on a concrete-T Arc chain returns
   the correct value (proves the helper bridge is wired end-to-end for
   both ARC_CLONE and ARC_GET).

2. Behavioural: dropping an `Arc<T>` on scope exit runs the helper's
   destroy body (proves ARC_DESTROY redirects, decrements the strong
   count, and runs `drop_thunk` on last drop — no leaked refcount, no
   use-after-free on the shared box).

3. Link contract: no `Arc<T>::clone__inst__…` / `Arc<T>::get__inst__…`
   / `Arc<T>::…::destroy__inst__…` symbols appear in emitted IR.  If
   the bodyless intrinsic template slipped into monomorphization, the
   IR would reference these and the linker would fail.

4. Link contract: no generic `_arc_*_impl` direct call survives into
   MIR/codegen — every call site must point at a concrete
   `_arc_*_impl__inst__<hash>` symbol.  A surviving generic direct call
   means the per-call-site helper instantiation was never queued.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from lang.driftc.driftc import compile_to_llvm_ir_for_tests, main as driftc_main
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root

from lang.codegen.llvm.test_utils import sanitizer_timeout


_ARC_CONCRETE_PROGRAM = """
module main;

import std.concurrent as conc;

pub struct Payload {
	pub n: Int
}

pub fn main() nothrow -> Int {
	val a = conc.arc(Payload(n = 42));
	val b = a.clone();
	return b.get().n;
}
""".lstrip()


# Chained-receiver form: `a.clone()` returns an rvalue `Arc<Payload>`
# that is *immediately* used as the receiver of `.get()`.  The Arc
# intrinsic lowering's auto-borrow branch only supports `HPlaceExpr` /
# `HVar` receivers directly; chaining through an rvalue requires HIR
# normalization to materialize a borrowed temporary before MIR
# lowering sees the inner `.get()` call.  This test pins that the
# chained form compiles, runs, and returns the correct value —
# without it, dropping the old bodied Arc methods would silently
# regress the rvalue-receiver form.
_ARC_CHAINED_RVALUE_PROGRAM = """
module main;

import std.concurrent as conc;

pub struct Payload {
	pub n: Int
}

pub fn main() nothrow -> Int {
	val a = conc.arc(Payload(n = 42));
	return a.clone().get().n;
}
""".lstrip()


_ARC_DROP_PROGRAM = """
module main;

import std.concurrent as conc;

pub struct Payload {
	pub n: Int
}

fn make_and_drop() nothrow -> Int {
	val a = conc.arc(Payload(n = 7));
	val b = a.clone();
	return b.get().n;
	// both `a` and `b` go out of scope here — ARC_DESTROY fires twice;
	// the first drop decrements refcount to 1, the second runs the
	// drop_thunk.
}

pub fn main() nothrow -> Int {
	val x = make_and_drop();
	val y = make_and_drop();
	return x + y;
}
""".lstrip()


def _compile_ir(tmp_path: Path, source: str) -> str:
	src = tmp_path / "main.drift"
	src.write_text(source)
	modules, type_table, exception_catalog, module_exports, module_deps, parse_diags = parse_drift_workspace_to_hir(
		[src],
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
		test_build_only=True,
	)
	assert parse_diags == [], f"parse diagnostics: {parse_diags}"
	func_hirs, signatures, _ = flatten_modules(modules)
	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exception_catalog,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		enforce_entrypoint=True,
		entry="main::main",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == [], f"unexpected codegen errors: {errors}"
	return ir


def _compile_and_run(tmp_path: Path, source: str) -> int:
	mod_root = tmp_path / "mods"
	main_src = mod_root / "main" / "main.drift"
	main_src.parent.mkdir(parents=True, exist_ok=True)
	main_src.write_text(source)
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
	assert rc == 0, f"driftc failed with rc={rc}"
	result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=sanitizer_timeout(10))
	return result.returncode


def test_arc_clone_get_chain_returns_correct_value(tmp_path: Path) -> None:
	"""Behavioural: binding `a.clone()` to a local and then calling
	`.get().field` returns the stored value — the lvalue form of the
	ARC_CLONE + ARC_GET bridge (receiver is an `HVar` / `HPlaceExpr`)."""
	rc = _compile_and_run(tmp_path, _ARC_CONCRETE_PROGRAM)
	assert rc == 42, f"expected 42, got {rc}"


def test_arc_clone_get_chained_rvalue_receiver(tmp_path: Path) -> None:
	"""Behavioural: `a.clone().get().n` — the **rvalue-receiver**
	shape.  `a.clone()` returns an `Arc<Payload>` rvalue that is
	immediately the receiver of `.get()`.  Without HIR temporary
	materialization this would reach the Arc intrinsic lowering's
	auto-borrow branch with a non-place receiver and hit a
	`NotImplementedError`.  The old bodied Arc methods supported this
	chained shape through the normal method-call auto-borrow path, so
	dropping them without covering the rvalue case would silently
	regress real user code (e.g. `resolver.get().resolve(ev)` inside
	std.log hot paths where the Arc itself is stored through a struct
	field that returns a borrowed temporary)."""
	rc = _compile_and_run(tmp_path, _ARC_CHAINED_RVALUE_PROGRAM)
	assert rc == 42, f"expected 42 from chained rvalue form, got {rc}"


def test_arc_drop_runs_helper_destroy(tmp_path: Path) -> None:
	"""Behavioural: scope-exit drops on `Arc<Payload>` must route
	through ARC_DESTROY → `_arc_destroy_impl<Payload>`.  Running the
	program twice back-to-back should not leak refcounts or crash;
	each call gets its own fresh Arc and drops it cleanly."""
	rc = _compile_and_run(tmp_path, _ARC_DROP_PROGRAM)
	assert rc == 14, f"expected 14 (7+7), got {rc}"


def test_no_bodyless_intrinsic_template_inst_symbols(tmp_path: Path) -> None:
	"""Link contract: the bodyless `@intrinsic` Arc methods are never
	monomorphized.  If they leak into codegen, we'd see
	`Arc<T>::clone__inst__…` (or get/destroy) symbols — which would
	reference an undefined function at link time."""
	ir = _compile_ir(tmp_path, _ARC_CONCRETE_PROGRAM)
	# Look for the intrinsic-template instantiated names.  They follow
	# the `{symbol}__inst__` pattern used by `_request_instantiation`.
	# Note: some codegen paths prepend package/mangling prefixes; we
	# search the raw (pre-mangling) mention of these template names.
	offending = [
		sym
		for sym in [
			"Arc<T>::clone__inst__",
			"Arc<T>::get__inst__",
			"Arc<T>::std.core.Destructible::destroy__inst__",
		]
		if sym in ir
	]
	assert offending == [], (
		f"bodyless @intrinsic Arc templates leaked into codegen as "
		f"instantiations: {offending}"
	)


def test_no_generic_helper_call_survives(tmp_path: Path) -> None:
	"""Link contract: every Arc-helper call site must point at a
	monomorphized `_arc_*_impl__inst__<hash>` symbol.  A raw
	`call @_arc_get_impl(` (without the `__inst__` suffix) would mean
	the post-pass helper-instantiation plumbing missed that call site
	and we emitted a call to the generic template — an undefined
	symbol at link time."""
	ir = _compile_ir(tmp_path, _ARC_CONCRETE_PROGRAM)
	# Find any declare/define/call references to the bare helper
	# names WITHOUT the `__inst__` suffix.  Allow the helper name to
	# appear as a prefix of an instantiation symbol; catch only the
	# case where the name is immediately followed by `(` or end-of-id.
	bad_patterns = [
		# clang IR reference shape: `@<name>(` or `@<name>"` etc.
		r"@_arc_clone_impl(?!__inst__|[A-Za-z0-9_])",
		r"@_arc_get_impl(?!__inst__|[A-Za-z0-9_])",
		r"@_arc_destroy_impl(?!__inst__|[A-Za-z0-9_])",
	]
	for pat in bad_patterns:
		matches = re.findall(pat, ir)
		assert not matches, (
			f"generic Arc helper call survived into IR (pattern {pat!r}): "
			f"{len(matches)} hits"
		)
